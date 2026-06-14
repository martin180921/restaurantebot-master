from fastapi import FastAPI, Request, BackgroundTasks, Response
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import os
import urllib.parse

load_dotenv()
app = FastAPI()

ACCOUNT_SID     = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN      = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER   = os.getenv("TWILIO_WHATSAPP_NUMBER")
APP_CLIENTE_URL = os.getenv(
    "APP_CLIENTE_URL", "https://app-client-production-3486.up.railway.app"
).rstrip("/")
# C4: validar la firma de Twilio salvo que se desactive a propósito (p. ej. en
# pruebas locales donde la URL pública no coincide con la que firma Twilio).
TWILIO_VALIDATE = os.getenv("TWILIO_VALIDATE", "true").lower() != "false"


# ── Config de base de datos (C7) ────────────────────────────────────────────────
def _normalizar_db_url(url):
    """Valida/normaliza DATABASE_URL: 'postgres://' → 'postgresql://'.

    Railway entrega el esquema 'postgres://' que SQLAlchemy 2.0 ya no acepta. Si
    falta, fallamos con un mensaje claro en vez de create_engine(None).
    """
    if not url:
        raise RuntimeError(
            "DATABASE_URL no está configurada. Define la variable de entorno con "
            "la cadena de conexión de PostgreSQL antes de arrancar el bot."
        )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


DATABASE_URL = _normalizar_db_url(os.getenv("DATABASE_URL"))
# C5: pre_ping descarta conexiones muertas y recycle las renueva antes del
# timeout del servidor (Railway corta las conexiones inactivas).
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)

# C4/C7: si faltan credenciales de Twilio no reventamos al importar; avisamos y
# degradamos con elegancia (init_db y /webhook siguen respondiendo).
if ACCOUNT_SID and AUTH_TOKEN:
    client    = Client(ACCOUNT_SID, AUTH_TOKEN)
    validator = RequestValidator(AUTH_TOKEN)
else:
    client    = None
    validator = None
    print("[WARN] Credenciales de Twilio incompletas; el bot no enviará mensajes.")

# ── Inicializar tablas ─────────────────────────────────────────────────────────
# El bot es el dueño del esquema: crea/actualiza las tablas en cada arranque.
def init_db():
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sesiones (
                numero      VARCHAR(50) PRIMARY KEY,
                estado      VARCHAR(30) NOT NULL DEFAULT 'inicio',
                carrito     TEXT        NOT NULL DEFAULT '[]',
                actualizado TIMESTAMP   NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS menu (
                id       SERIAL PRIMARY KEY,
                nombre   VARCHAR(100) NOT NULL,
                precio   INTEGER NOT NULL,
                activo   BOOLEAN NOT NULL DEFAULT TRUE,
                orden    INTEGER NOT NULL DEFAULT 0
            )
        """))
        count = conn.execute(text("SELECT COUNT(*) FROM menu")).scalar()
        if count == 0:
            conn.execute(text("""
                INSERT INTO menu (nombre, precio, activo, orden) VALUES
                ('Hamburguesa', 25000, TRUE, 1),
                ('Pizza',       35000, TRUE, 2),
                ('Ensalada',    18000, TRUE, 3)
            """))

        # ── Mesas: gestión dinámica de mesas del restaurante ───────────────────
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS mesas (
                id      SERIAL PRIMARY KEY,
                nombre  VARCHAR(50)  NOT NULL,
                activa  BOOLEAN      NOT NULL DEFAULT TRUE,
                creada  TIMESTAMP    NOT NULL DEFAULT NOW()
            )
        """))

        # ── Pedidos: creado explícitamente aquí (antes no lo creaba ningún código)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pedidos (
                id                  SERIAL PRIMARY KEY,
                numero_cliente      VARCHAR(50)  NOT NULL,
                items               TEXT         NOT NULL,
                total               INTEGER      NOT NULL,
                estado              VARCHAR(30)  NOT NULL DEFAULT 'pendiente',
                fecha               TIMESTAMP    NOT NULL DEFAULT NOW(),
                mesa_id             INTEGER      REFERENCES mesas(id),
                motivo_cancelacion  TEXT
            )
        """))
        # Actualiza tablas 'pedidos' preexistentes que aún no tienen estas columnas
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mesa_id INTEGER REFERENCES mesas(id)"
        ))
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS motivo_cancelacion TEXT"
        ))

        conn.commit()

init_db()


# ── Webhook ────────────────────────────────────────────────────────────────────
# El bot ya no procesa pedidos por texto. A cualquier mensaje responde con el
# enlace a la carta digital (app_cliente), donde el cliente elige su mesa, arma
# el carrito y lo envía directo a la cocina.
def _url_publica(request: Request) -> str:
    """URL con la que Twilio firmó la petición.

    Detrás del proxy de Railway el esquema interno es http, pero Twilio firma con
    la URL pública https; corregimos el esquema con X-Forwarded-Proto.
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return str(request.url.replace(scheme=proto))


@app.post("/webhook")
async def recibir_mensaje(request: Request, background_tasks: BackgroundTasks):
    form   = await request.form()
    params = dict(form)

    # C4: rechaza cualquier POST que no provenga de Twilio (firma HMAC en la
    # cabecera X-Twilio-Signature). Sin esto, cualquiera podía disparar envíos.
    if TWILIO_VALIDATE and validator is not None:
        firma = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(_url_publica(request), params, firma):
            return Response(status_code=403)

    numero = params.get("From", "")
    if numero:
        # C4: la llamada a Twilio es bloqueante; la lanzamos en segundo plano para
        # no frenar el event loop ni demorar el 200 (si tardamos, Twilio reintenta
        # y se enviaban bienvenidas duplicadas).
        background_tasks.add_task(enviar_mensaje, numero, mensaje_bienvenida(numero))
    return {"status": "ok"}


def mensaje_bienvenida(numero: str) -> str:
    tel  = numero.replace("whatsapp:", "").strip()
    link = f"{APP_CLIENTE_URL}/?tel={urllib.parse.quote(tel)}"
    return (
        "¡Hola! 👋 Bienvenido a *RestauranteBOT*.\n\n"
        "📲 Haz tu pedido desde nuestra carta digital:\n"
        f"{link}\n\n"
        "Elige tu mesa, arma tu pedido y la cocina lo recibirá al instante. "
        "¡Gracias!"
    )


# ── Enviar mensaje WhatsApp ────────────────────────────────────────────────────
def enviar_mensaje(numero: str, texto: str):
    if client is None or not TWILIO_NUMBER:
        print("[WARN] Twilio no configurado; no se envió el mensaje.")
        return
    try:
        client.messages.create(
            from_=f"whatsapp:{TWILIO_NUMBER}",
            body=texto,
            to=numero,
        )
    except Exception as e:
        # Corre como background task tras responder 200, así que no propagamos.
        print(f"[ERROR] No se pudo enviar el WhatsApp: {e}")
