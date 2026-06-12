from fastapi import FastAPI, Request
from twilio.rest import Client
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import os
import json

load_dotenv()
app = FastAPI()

ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
DATABASE_URL  = os.getenv("DATABASE_URL")

client = Client(ACCOUNT_SID, AUTH_TOKEN)
engine = create_engine(DATABASE_URL)

# ── Inicializar tablas ─────────────────────────────────────────────────────────
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
        conn.commit()

init_db()

# ── Menú desde DB ──────────────────────────────────────────────────────────────
def cargar_menu() -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, nombre, precio FROM menu WHERE activo = TRUE ORDER BY orden, id"
        )).fetchall()
    return {
        str(i + 1): {"id": row.id, "nombre": row.nombre, "precio": row.precio}
        for i, row in enumerate(rows)
    }

# ── CRUD de sesión ─────────────────────────────────────────────────────────────
def get_sesion(conn, numero: str) -> dict:
    row = conn.execute(
        text("SELECT estado, carrito FROM sesiones WHERE numero = :n"),
        {"n": numero}
    ).fetchone()
    if row:
        return {"estado": row.estado, "carrito": json.loads(row.carrito)}
    return {"estado": "inicio", "carrito": []}


def set_sesion(conn, numero: str, estado: str, carrito: list):
    conn.execute(text("""
        INSERT INTO sesiones (numero, estado, carrito, actualizado)
        VALUES (:n, :e, :c, NOW())
        ON CONFLICT (numero) DO UPDATE
        SET estado = EXCLUDED.estado,
            carrito = EXCLUDED.carrito,
            actualizado = NOW()
    """), {"n": numero, "e": estado, "c": json.dumps(carrito)})


def delete_sesion(conn, numero: str):
    conn.execute(text("DELETE FROM sesiones WHERE numero = :n"), {"n": numero})


# ── Webhook ────────────────────────────────────────────────────────────────────
@app.post("/webhook")
async def recibir_mensaje(request: Request):
    data   = await request.form()
    numero = data.get("From")
    texto  = data.get("Body", "").strip()

    respuesta = procesar_mensaje(texto, numero)
    enviar_mensaje(numero, respuesta)
    return {"status": "ok"}


# ── Lógica del bot ─────────────────────────────────────────────────────────────
def procesar_mensaje(texto: str, numero: str) -> str:
    texto = texto.lower().strip()
    MENU  = cargar_menu()

    with engine.connect() as conn:
        sesion  = get_sesion(conn, numero)
        estado  = sesion["estado"]
        carrito = sesion["carrito"]

        # ── Saludo / inicio ────────────────────────────────────────────────────
        if texto in ["hola", "buenas", "menu", "menú", "inicio"]:
            set_sesion(conn, numero, "en_menu", [])
            conn.commit()
            return mostrar_menu(MENU)

        # ── Selección de ítem ──────────────────────────────────────────────────
        if texto in MENU and estado in ("en_menu", "agregando"):
            item = MENU[texto]
            carrito.append({"id": texto, "nombre": item["nombre"], "precio": item["precio"]})
            set_sesion(conn, numero, "agregando", carrito)
            conn.commit()
            return resumen_carrito(carrito)

        # ── Iniciar flujo de quitar ítem ───────────────────────────────────────
        if texto == "quitar":
            if not carrito:
                return "Tu carrito está vacío. No hay nada que quitar."
            set_sesion(conn, numero, "quitando", carrito)
            conn.commit()
            return menu_quitar(carrito)

        # ── Selección de ítem a quitar ─────────────────────────────────────────
        if estado == "quitando":
            unicos = lista_unicos(carrito)

            if texto.isdigit() and 1 <= int(texto) <= len(unicos):
                idx = int(texto) - 1
                nombre_quitar = unicos[idx]

                for i, item in enumerate(carrito):
                    if item["nombre"] == nombre_quitar:
                        carrito.pop(i)
                        break

                if not carrito:
                    delete_sesion(conn, numero)
                    conn.commit()
                    return "Carrito vacío. Escribe 'menu' para empezar de nuevo."

                set_sesion(conn, numero, "agregando", carrito)
                conn.commit()
                return f"✅ {nombre_quitar} eliminado.\n\n" + resumen_carrito(carrito)

            elif texto == "cancelar":
                set_sesion(conn, numero, "agregando", carrito)
                conn.commit()
                return resumen_carrito(carrito)

            else:
                return f"Opción no válida. Responde con un número del 1 al {len(unicos)}, o escribe *cancelar*.\n\n" + menu_quitar(carrito)

        # ── Confirmar pedido ───────────────────────────────────────────────────
        if texto == "confirmar":
            if not carrito:
                return "No tienes ítems en tu pedido. Escribe 'menu' para empezar."
            total = sum(i["precio"] for i in carrito)
            items_str = json.dumps(carrito, ensure_ascii=False)
            guardar_pedido(numero, items_str, total)
            delete_sesion(conn, numero)
            conn.commit()
            return (
                f"✅ Pedido confirmado!\n"
                f"Total: ${total:,.0f}\n"
                f"Tiempo estimado: 25 minutos. ¡Gracias!"
            )

        # ── Cancelar pedido ────────────────────────────────────────────────────
        if texto == "cancelar":
            delete_sesion(conn, numero)
            conn.commit()
            return "Pedido cancelado. Escribe 'menu' cuando quieras empezar de nuevo."

        # ── Ver carrito ────────────────────────────────────────────────────────
        if texto in ["carrito", "ver pedido", "mi pedido"]:
            if not carrito:
                return "Tu carrito está vacío. Escribe 'menu' para ver opciones."
            return resumen_carrito(carrito)

        # ── Fallback con menú ──────────────────────────────────────────────────
        if estado in ("en_menu", "agregando"):
            return (
                "❌ Opción no válida.\n\n"
                + mostrar_menu(MENU)
                + "\n\nSi deseas eliminar un ítem escribe *quitar*."
            )

        return "Escribe *menu* para comenzar."


# ── Helpers de presentación ────────────────────────────────────────────────────
def mostrar_menu(MENU: dict) -> str:
    lineas = ["🍽️ *RestauranteBOT*\n¿Qué deseas pedir?\n"]
    for key, item in MENU.items():
        lineas.append(f"{key}. {item['nombre']} - ${item['precio']:,.0f}")
    lineas.append("\nResponde con el número. Puedes agregar varios ítems.")
    lineas.append("Escribe *confirmar* para finalizar o *cancelar* para borrar.")
    return "\n".join(lineas)


def resumen_carrito(carrito: list) -> str:
    lineas = ["🛒 *Tu pedido hasta ahora:*\n"]
    conteo = {}
    for item in carrito:
        nombre = item["nombre"]
        if nombre not in conteo:
            conteo[nombre] = {"qty": 0, "precio": item["precio"]}
        conteo[nombre]["qty"] += 1

    for nombre, datos in conteo.items():
        subtotal = datos["qty"] * datos["precio"]
        lineas.append(f"• {datos['qty']}x {nombre} — ${subtotal:,.0f}")

    total = sum(i["precio"] for i in carrito)
    lineas.append(f"\n💰 *Total: ${total:,.0f}*")
    lineas.append("\nAgrega más, escribe *quitar*, *confirmar* o *cancelar*.")
    return "\n".join(lineas)


def lista_unicos(carrito: list) -> list:
    vistos = []
    for item in carrito:
        if item["nombre"] not in vistos:
            vistos.append(item["nombre"])
    return vistos


def menu_quitar(carrito: list) -> str:
    unicos = lista_unicos(carrito)
    lineas = ["¿Qué ítem quieres quitar? (se elimina una unidad)\n"]
    for i, nombre in enumerate(unicos, 1):
        qty = sum(1 for x in carrito if x["nombre"] == nombre)
        lineas.append(f"{i}. {nombre} (x{qty})")
    lineas.append("\nResponde con el número o escribe *cancelar*.")
    return "\n".join(lineas)


# ── Guardar pedido confirmado ──────────────────────────────────────────────────
def guardar_pedido(numero: str, items: str, total: int):
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO pedidos (numero_cliente, items, total, estado)
            VALUES (:numero, :items, :total, 'pendiente')
        """), {"numero": numero, "items": items, "total": total})
        conn.commit()


# ── Enviar mensaje WhatsApp ────────────────────────────────────────────────────
def enviar_mensaje(numero: str, texto: str):
    client.messages.create(
        from_=f"whatsapp:{TWILIO_NUMBER}",
        body=texto,
        to=numero
    )
