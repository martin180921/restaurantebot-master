"""Conexión compartida a la base de datos y lecturas comunes entre vistas."""
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import streamlit as st
import pandas as pd
import os

load_dotenv()


# ── Config de conexión (C7) ─────────────────────────────────────────────────────
def _normalizar_db_url(url):
    """Valida y normaliza DATABASE_URL.

    Railway/Heroku entregan el esquema 'postgres://', que SQLAlchemy 2.0 ya no
    acepta; lo reescribimos a 'postgresql://'. Si falta, fallamos con un mensaje
    claro en vez de un error opaco de create_engine(None).
    """
    if not url:
        raise RuntimeError(
            "DATABASE_URL no está configurada. Define la variable de entorno con "
            "la cadena de conexión de PostgreSQL antes de arrancar el panel."
        )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


DATABASE_URL = _normalizar_db_url(os.getenv("DATABASE_URL"))
# C5: pre_ping descarta conexiones muertas (Railway corta las inactivas) y
# pool_recycle las renueva antes del timeout del servidor → sin 500s aleatorios.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)


# ── Formato de moneda LATAM $XX.XXX (C6) ────────────────────────────────────────
def fmt_money(valor) -> str:
    """Formatea un monto entero con punto de miles: 35000 → '35.000'.

    Devuelve solo el número; el símbolo '$' se antepone en cada vista.
    """
    try:
        return f"{int(round(float(valor))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


# ── Fechas en español, sin depender del locale ni de %-d (U4) ───────────────────
# %-d y %B/%b dependen de glibc (revientan en Windows) y del locale (devuelven
# meses en inglés). Formateamos a mano para que sea portable y en español.
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
_MESES_ABBR = ["ene", "feb", "mar", "abr", "may", "jun", "jul",
               "ago", "sep", "oct", "nov", "dic"]


def fecha_larga(dt) -> str:
    """'13 de junio, 2026'."""
    return f"{dt.day} de {_MESES[dt.month - 1]}, {dt.year}"


def fecha_corta(dt) -> str:
    """'13 jun · 14:30'."""
    return f"{dt.day} {_MESES_ABBR[dt.month - 1]} · {dt.hour:02d}:{dt.minute:02d}"


# ── Menú (lectura compartida por views/menu.py y views/nuevo_pedido.py) ────────
# P1: el menú cambia poco; lo cacheamos para no consultar la BD en cada rerun
# (cada tap de +/- dispara un rerun). menu.py llama cargar_menu.clear() tras
# cada escritura para reflejar los cambios al instante.
@st.cache_data(ttl=60)
def cargar_menu():
    with engine.connect() as conn:
        resultado = conn.execute(text(
            "SELECT id, nombre, precio, activo, orden FROM menu ORDER BY orden, id"
        ))
        return pd.DataFrame(resultado.fetchall(), columns=resultado.keys())
