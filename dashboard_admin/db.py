"""Conexión compartida a la base de datos y lecturas comunes entre vistas."""
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import pandas as pd
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)


# ── Menú (lectura compartida por views/menu.py y views/nuevo_pedido.py) ────────
def cargar_menu():
    with engine.connect() as conn:
        resultado = conn.execute(text(
            "SELECT id, nombre, precio, activo, orden FROM menu ORDER BY orden, id"
        ))
        return pd.DataFrame(resultado.fetchall(), columns=resultado.keys())
