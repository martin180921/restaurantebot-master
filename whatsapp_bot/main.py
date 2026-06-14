from fastapi import FastAPI, Request
from twilio.rest import Client
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import os
import urllib.parse

load_dotenv()
app = FastAPI()

ACCOUNT_SID     = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN      = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER   = os.getenv("TWILIO_WHATSAPP_NUMBER")
DATABASE_URL    = os.getenv("DATABASE_URL")
APP_CLIENTE_URL = os.getenv(
    "APP_CLIENTE_URL", "https://app-client-production-3486.up.railway.app"
).rstrip("/")

client = Client(ACCOUNT_SID, AUTH_TOKEN)
engine = create_engine(DATABASE_URL)

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
                id              SERIAL PRIMARY KEY,
                numero_cliente  VARCHAR(50)  NOT NULL,
                items           TEXT         NOT NULL,
                total           INTEGER      NOT NULL,
                estado          VARCHAR(30)  NOT NULL DEFAULT 'pendiente',
                fecha           TIMESTAMP    NOT NULL DEFAULT NOW(),
                mesa_id         INTEGER      REFERENCES mesas(id)
            )
        """))
        # Actualiza tablas 'pedidos' preexistentes que aún no tienen mesa_id
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mesa_id INTEGER REFERENCES mesas(id)"
        ))

        conn.commit()

init_db()


# ── Webhook ────────────────────────────────────────────────────────────────────
# El bot ya no procesa pedidos por texto. A cualquier mensaje responde con el
# enlace a la carta digital (app_cliente), donde el cliente elige su mesa, arma
# el carrito y lo envía directo a la cocina.
@app.post("/webhook")
async def recibir_mensaje(request: Request):
    data   = await request.form()
    numero = data.get("From", "")
    enviar_mensaje(numero, mensaje_bienvenida(numero))
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
    client.messages.create(
        from_=f"whatsapp:{TWILIO_NUMBER}",
        body=texto,
        to=numero
    )
