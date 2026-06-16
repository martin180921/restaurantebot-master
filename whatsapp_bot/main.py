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
                id            SERIAL PRIMARY KEY,
                nombre        VARCHAR(100) NOT NULL,
                precio        INTEGER NOT NULL,
                activo        BOOLEAN NOT NULL DEFAULT TRUE,
                orden         INTEGER NOT NULL DEFAULT 0,
                agotado_hasta DATE
            )
        """))
        # F6: "86 / agotado hoy" — disponible de nuevo automáticamente al día siguiente
        conn.execute(text(
            "ALTER TABLE menu ADD COLUMN IF NOT EXISTS agotado_hasta DATE"
        ))
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
                motivo_cancelacion  TEXT,
                pagado              BOOLEAN      NOT NULL DEFAULT FALSE,
                total_pagado        INTEGER      NOT NULL DEFAULT 0
            )
        """))
        # Actualiza tablas 'pedidos' preexistentes que aún no tienen estas columnas
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mesa_id INTEGER REFERENCES mesas(id)"
        ))
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS motivo_cancelacion TEXT"
        ))
        # pagado: dimensión de cobro independiente del estado de cocina (el monitor
        # de mesas marca 'pagado' sin tocar el flujo pendiente→…→entregado).
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pagado BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        # total_pagado: monto abonado hasta ahora (libro acumulado para pagos
        # parciales / cuentas divididas). saldo = total − total_pagado. Cuando cubre
        # el total, el cobro marca además pagado=TRUE. Los pedidos ya pagados antes
        # de esta columna tienen total_pagado=0 pero pagado=TRUE → el panel los trata
        # como cobrados por completo (ver db.saldo_pedido/cobrado_pedido).
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS total_pagado INTEGER NOT NULL DEFAULT 0"
        ))

        # ── Pagos: libro de abonos (uno por pedido tocado en cada cobro) ────────
        # Detalle del cobro que 'pedidos.total_pagado' resume: método (efectivo /
        # transferencia) y hora REAL del pago. Fuente para el desglose de caja por
        # método; total_pagado se mantiene denormalizado para el saldo en cada render.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pagos (
                id         SERIAL PRIMARY KEY,
                pedido_id  INTEGER     NOT NULL REFERENCES pedidos(id),
                monto      INTEGER     NOT NULL,
                metodo     VARCHAR(20) NOT NULL DEFAULT 'efectivo',
                fecha      TIMESTAMP   NOT NULL DEFAULT NOW()
            )
        """))

        # ── Turnos de caja: arqueo (apertura con fondo, cierre con conteo) ──────
        # Esperado = fondo_inicial + efectivo cobrado entre abierto y cerrado (de
        # 'pagos'); diferencia = efectivo_contado − esperado. Las transferencias no
        # entran a la caja física.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS turnos_caja (
                id               SERIAL PRIMARY KEY,
                abierto          TIMESTAMP NOT NULL DEFAULT NOW(),
                cerrado          TIMESTAMP,
                fondo_inicial    INTEGER   NOT NULL DEFAULT 0,
                efectivo_contado INTEGER,
                nota             TEXT
            )
        """))

        # ════════════════════════════════════════════════════════════════════
        # OVERHAUL DEL MENÚ (aditivo): secciones (Plato del Día / Especiales /
        # A la carta / Bebidas), opciones del Plato del Día, ajustes de precios y
        # recargo de entrega, base de clientes y metadatos de entrega en pedidos.
        # NADA se elimina: el menú y los pedidos existentes siguen funcionando
        # (categoria default 'a_la_carta', tipo_entrega NULL = pedido de mesa).
        # ════════════════════════════════════════════════════════════════════

        # Componentes del Plato del Día. Cada fila es una opción toggleable mapeada
        # a un grupo (entrada/principio/proteina/acompanamiento). Las sopas son
        # filas 'entrada' que el restaurante activa/desactiva por día y se listan
        # como pares de Fruta/Huevo. Reusa el patrón "86" (agotado_hasta).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS menu_componentes (
                id            SERIAL PRIMARY KEY,
                grupo         VARCHAR(20)  NOT NULL,
                nombre        VARCHAR(100) NOT NULL,
                activo        BOOLEAN      NOT NULL DEFAULT TRUE,
                orden         INTEGER      NOT NULL DEFAULT 0,
                agotado_hasta DATE
            )
        """))
        # Semilla SOLO en la primera inicialización (tabla vacía): así no se
        # "resucitan" opciones que el restaurante haya borrado en redeploys.
        if conn.execute(text("SELECT COUNT(*) FROM menu_componentes")).scalar() == 0:
            conn.execute(text("""
                INSERT INTO menu_componentes (grupo, nombre, orden) VALUES
                ('entrada', 'Fruta', 1), ('entrada', 'Huevo', 2), ('entrada', 'Sopa del día', 3),
                ('principio', 'Frijol', 1), ('principio', 'Lenteja', 2),
                ('proteina', 'Res', 1), ('proteina', 'Cerdo', 2), ('proteina', 'Pechuga', 3),
                ('acompanamiento', 'Arroz', 1), ('acompanamiento', 'Maduro', 2),
                ('acompanamiento', 'Papa', 3), ('acompanamiento', 'Ensalada', 4)
            """))

        # Categoría + descripción del catálogo 'menu': especiales (con resumen de
        # lo que incluyen), a la carta y bebidas. Las filas existentes quedan como
        # 'a_la_carta' sin tocar su precio ni su estado.
        conn.execute(text(
            "ALTER TABLE menu ADD COLUMN IF NOT EXISTS categoria VARCHAR(20) NOT NULL DEFAULT 'a_la_carta'"
        ))
        conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS descripcion TEXT"))

        # Ajustes clave/valor: precios planos (Plato del Día y Especiales), recargo
        # de entrega (Domicilio/Para Llevar) y nº de acompañamientos a elegir.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ajustes (
                clave VARCHAR(50) PRIMARY KEY,
                valor TEXT        NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO ajustes (clave, valor) VALUES
            ('plato_dia_precio',  '18000'),
            ('especiales_precio', '25000'),
            ('fee_entrega',       '4000'),
            ('acompanamientos_n', '3')
            ON CONFLICT (clave) DO NOTHING
        """))

        # Base de clientes: la alimenta la app pública (tel como identidad).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS clientes (
                telefono    VARCHAR(40) PRIMARY KEY,
                nombre      VARCHAR(120),
                direccion   TEXT,
                creado      TIMESTAMP NOT NULL DEFAULT NOW(),
                actualizado TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))

        # Metadatos de entrega en 'pedidos' (web: domicilio/para_llevar; los de
        # mesa dejan tipo_entrega NULL). 'fee' es el recargo plano de entrega;
        # 'paga_con' el efectivo con el que paga el cliente (para el cambio).
        for _col, _ddl in [
            ("tipo_entrega",     "VARCHAR(15)"),
            ("cliente_nombre",   "VARCHAR(120)"),
            ("cliente_telefono", "VARCHAR(40)"),
            ("direccion",        "TEXT"),
            ("metodo_pago",      "VARCHAR(20)"),
            ("paga_con",         "INTEGER"),
            ("fee",              "INTEGER NOT NULL DEFAULT 0"),
            ("nota_general",     "TEXT"),
        ]:
            conn.execute(text(f"ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS {_col} {_ddl}"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_pedidos_tipo_entrega ON pedidos (tipo_entrega)"
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
        "📲 Haz tu pedido a domicilio o para llevar desde nuestra carta digital:\n"
        f"{link}\n\n"
        "Elige cómo lo quieres, arma tu pedido y nosotros nos encargamos. "
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
