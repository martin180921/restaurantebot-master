"""Conexión compartida a la base de datos y lecturas comunes entre vistas."""
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from datetime import date
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

# Identidad del inquilino (SaaS multi-tenant). Aún no hay tabla de restaurantes ni
# login por tenant, así que de momento la tomamos del entorno (default 1 = single
# tenant). Cuando exista auth por restaurante, esto saldrá de la sesión. El agente
# local filtra por este mismo id en su config.json.
RESTAURANTE_ID = int(os.getenv("RESTAURANTE_ID", "1"))
# C5: pre_ping descarta conexiones muertas (Railway corta las inactivas) y
# pool_recycle las renueva antes del timeout del servidor → sin 500s aleatorios.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)


# ── Esquema defensivo (F1/F6) ───────────────────────────────────────────────────
# El bot es el dueño del esquema, pero el panel puede arrancar antes. Garantizamos
# las columnas que necesitan sus flujos (motivo de cancelación, "agotado hoy") una
# sola vez al importar este módulo (proceso del panel). Idempotente.
def _ensure_schema():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS motivo_cancelacion TEXT"))
        # cancelled_at: marca de tiempo de la cancelación, para el historial del
        # administrador (agrupa los cancelados por día). Se rellena al cancelar; los
        # cancelados previos a esta columna quedan NULL y caen bajo su 'fecha' de creación.
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS agotado_hasta DATE"))
        # pagado: cobro independiente del estado de cocina (lo usa el monitor de mesas).
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pagado BOOLEAN NOT NULL DEFAULT FALSE"))
        # total_pagado: libro acumulado de abonos para pagos parciales / cuentas
        # divididas (saldo = total − total_pagado). Canónico en el bot; defensivo aquí.
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS total_pagado INTEGER NOT NULL DEFAULT 0"))
        # pagos: libro de abonos (método + hora real de pago). Canónico en el bot;
        # defensivo aquí para que el panel cobre y desglose por método pre-redeploy.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pagos (
                id         SERIAL PRIMARY KEY,
                pedido_id  INTEGER     NOT NULL REFERENCES pedidos(id),
                monto      INTEGER     NOT NULL,
                metodo     VARCHAR(20) NOT NULL DEFAULT 'efectivo',
                fecha      TIMESTAMP   NOT NULL DEFAULT NOW()
            )
        """))
        # Transferencias detalladas: submetodo (nequi/daviplata/breb) y comprobante
        # (n.º de transacción). NULL en efectivo. Canónico en el bot; defensivo aquí.
        conn.execute(text("ALTER TABLE pagos ADD COLUMN IF NOT EXISTS submetodo VARCHAR(20)"))
        conn.execute(text("ALTER TABLE pagos ADD COLUMN IF NOT EXISTS comprobante VARCHAR(60)"))
        # pago_lineas: libro auxiliar de "pagar por plato" — cuántas unidades de cada
        # línea (índice en pedidos.items) ya están pagadas. 'total_pagado' sigue siendo
        # la autoridad del saldo; esto solo recuerda QUÉ unidades se cobraron para el
        # checklist. Invariante en modo por-plato: total_pagado == Σ(cantidad_pagada×precio).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pago_lineas (
                pedido_id       INTEGER NOT NULL REFERENCES pedidos(id),
                linea_idx       INTEGER NOT NULL,
                cantidad_pagada INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (pedido_id, linea_idx)
            )
        """))
        # claves_mesero: PINs de turno efímeros del mesero (acceso solo-PIN, sin
        # contraseña fija). Se generan en caja, se revocan a mano o al cerrar la caja.
        # Guardamos solo el hash; el PIN se muestra una sola vez al generarlo.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS claves_mesero (
                id         SERIAL PRIMARY KEY,
                etiqueta   VARCHAR(120),
                clave_hash VARCHAR(64) NOT NULL,
                activa     BOOLEAN     NOT NULL DEFAULT TRUE,
                creada     TIMESTAMP   NOT NULL DEFAULT NOW(),
                revocada   TIMESTAMP,
                creada_por VARCHAR(20)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_claves_mesero_activa ON claves_mesero (activa)"
        ))
        # turnos_caja: arqueo de caja v1 (apertura con fondo, cierre con conteo).
        # Reemplazada por cierres_caja (abajo); la dejamos definida de forma defensiva
        # para no perder datos de turnos ya cerrados en despliegues previos.
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
        # cierres_caja: arqueo de turno v2 (cierre de caja). Apertura con base en
        # efectivo, y al cerrar se congela lo esperado vs. lo contado (efectivo y
        # transferencias verificadas en banco) más la diferencia de caja. Como mucho
        # un turno con estado='abierto' a la vez. Canónico en el bot; defensivo aquí.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cierres_caja (
                id                     SERIAL      PRIMARY KEY,
                fecha_apertura         TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
                fecha_cierre           TIMESTAMP,
                monto_apertura         INTEGER     NOT NULL,
                efectivo_esperado      INTEGER     NOT NULL DEFAULT 0,
                transferencia_esperada INTEGER     NOT NULL DEFAULT 0,
                efectivo_real          INTEGER,
                transferencia_real     INTEGER,
                diferencia             INTEGER     DEFAULT 0,
                estado                 VARCHAR(10) NOT NULL DEFAULT 'abierto'
            )
        """))
        # movimientos_caja: flujo de efectivo del cajón fuera de las ventas (gastos de
        # caja con su devolución de cambio, y base de cambio del repartidor con el float
        # devuelto al volver). 'estado'='abierto' = dinero aún afuera; 'cerrado' = ya
        # conciliado. 'ref_id' enlaza el retorno con su salida (reingreso_gasto→gasto,
        # retorno_base→base_repartidor). 'pedidos_ref' (JSON de ids) lista los pedidos de
        # domicilio que el repartidor lleva, para cobrarlos al volver (esos cobros entran
        # por el libro 'pagos', NO por aquí → sin doble conteo). Va DESPUÉS de cierres_caja.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS movimientos_caja (
                id           SERIAL       PRIMARY KEY,
                cierre_id    INTEGER      REFERENCES cierres_caja(id),
                tipo         VARCHAR(20)  NOT NULL,
                monto        INTEGER      NOT NULL,
                motivo       TEXT,
                actor_rol    VARCHAR(20),
                actor_nombre VARCHAR(120),
                ref_id       INTEGER,
                pedidos_ref  TEXT,
                estado       VARCHAR(15)  NOT NULL DEFAULT 'abierto',
                creado_at    TIMESTAMP    NOT NULL DEFAULT NOW()
            )
        """))
        # Defensivo si la tabla se creó antes de existir esta columna.
        conn.execute(text("ALTER TABLE movimientos_caja ADD COLUMN IF NOT EXISTS pedidos_ref TEXT"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_movimientos_caja_cierre "
            "ON movimientos_caja (cierre_id, tipo, estado)"
        ))
        # print_jobs: cola de impresión multi-tenant. El panel (cloud/Railway) encola
        # filas 'pendiente'; el Agente de Impresión Local de cada restaurante hace
        # polling por (restaurante_id, estado) y las imprime en su Epson 80mm. 'payload'
        # es JSONB con el ticket ya armado (ítems, totales, abrir_cajon, etc.).
        # 'error_msg' (fuera de la spec original) guarda el último fallo del agente.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS print_jobs (
                id             SERIAL      PRIMARY KEY,
                restaurante_id INTEGER     NOT NULL,
                tipo           VARCHAR(20) NOT NULL,
                payload        JSONB       NOT NULL,
                estado         VARCHAR(15) NOT NULL DEFAULT 'pendiente',
                intentos       INTEGER     NOT NULL DEFAULT 0,
                error_msg      TEXT,
                creado_at      TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
                impreso_at     TIMESTAMP
            )
        """))
        # Índice de polling del agente: WHERE restaurante_id = ? AND estado = 'pendiente'.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_print_jobs_tenant_estado "
            "ON print_jobs (restaurante_id, estado)"
        ))

        # ── OVERHAUL DEL MENÚ (aditivo, defensivo) ───────────────────────────
        # El bot es el dueño canónico del esquema y siembra los datos; aquí solo
        # garantizamos que las tablas/columnas EXISTAN para que el panel lea sin
        # romperse si arranca antes del redeploy del bot. NO sembramos componentes
        # (los siembra el bot) para no recrear opciones que el restaurante borró.
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
        conn.execute(text(
            "ALTER TABLE menu ADD COLUMN IF NOT EXISTS categoria VARCHAR(20) NOT NULL DEFAULT 'a_la_carta'"
        ))
        conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS descripcion TEXT"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ajustes (
                clave VARCHAR(50) PRIMARY KEY,
                valor TEXT        NOT NULL
            )
        """))
        # Ajustes: sí sembramos defaults (ON CONFLICT DO NOTHING, sin resurrección)
        # para que precios/recargo tengan valor aunque el panel arranque primero.
        conn.execute(text("""
            INSERT INTO ajustes (clave, valor) VALUES
            ('plato_dia_precio',  '18000'),
            ('especiales_precio', '25000'),
            ('fee_entrega',       '4000'),
            ('acompanamientos_n', '3')
            ON CONFLICT (clave) DO NOTHING
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS clientes (
                telefono    VARCHAR(40) PRIMARY KEY,
                nombre      VARCHAR(120),
                direccion   TEXT,
                creado      TIMESTAMP NOT NULL DEFAULT NOW(),
                actualizado TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
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

try:
    _ensure_schema()
except Exception:
    pass  # tablas aún sin crear (deploy nuevo): el bot las creará con las columnas


# ── Toasts no bloqueantes (compartidos por todas las vistas) ────────────────────
# st.toast NO sobrevive a un st.rerun() (el rerun descarta los deltas del run en
# curso) y casi toda acción del panel termina en st.rerun(). Por eso encolamos el
# toast en session_state y lo emitimos al inicio del run siguiente con
# drain_toasts() — lo llama panel.py antes de pintar la vista, y nuevo_pedido en
# su fragment (que se relanza con scope="fragment", sin re-ejecutar panel.py).
# Así el aviso flota sin desplazar el layout hacia abajo.
def flash(mensaje: str, icono: str = "✅") -> None:
    st.session_state.setdefault("_toasts", []).append((mensaje, icono))

def drain_toasts() -> None:
    for mensaje, icono in st.session_state.pop("_toasts", []):
        st.toast(mensaje, icon=icono)


# ── Formato de moneda LATAM $XX.XXX (C6) ────────────────────────────────────────
def fmt_money(valor) -> str:
    """Formatea un monto entero con punto de miles: 35000 → '35.000'.

    Devuelve solo el número; el símbolo '$' se antepone en cada vista.
    """
    try:
        return f"{int(round(float(valor))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


# ── Saldo / cobrado de un pedido (pagos parciales) ──────────────────────────────
# 'total_pagado' (libro acumulado de abonos) permite cuentas divididas sin destruir
# 'total'. saldo = total − total_pagado; cobrado = total_pagado (acotado a [0,total]).
# CLAVE de migración: los pedidos cobrados ANTES de existir total_pagado tienen
# total_pagado=0 pero pagado=TRUE, así que un pedido pagado se considera cobrado por
# completo SIEMPRE (saldo 0), sin importar total_pagado. Aceptan filas pandas o dict.
def _a_entero(valor) -> int:
    try:
        return int(round(float(valor)))
    except (TypeError, ValueError):
        return 0


def _es_pagado(valor) -> bool:
    """bool seguro para 'pagado': None/NaN/ausente → False (NaN es 'truthy' en Python)."""
    if valor is None:
        return False
    try:
        if pd.isna(valor):
            return False
    except (TypeError, ValueError):
        pass
    return bool(valor)


def saldo_pedido(row) -> int:
    """Saldo pendiente de un pedido = total − total_pagado, nunca negativo.
    Un pedido con pagado=TRUE siempre tiene saldo 0 (incl. los pagados pre-migración)."""
    if _es_pagado(row.get("pagado", False)):
        return 0
    return max(0, _a_entero(row.get("total", 0)) - _a_entero(row.get("total_pagado", 0)))


def cobrado_pedido(row) -> int:
    """Dinero realmente cobrado de un pedido. pagado=TRUE → total completo (cubre los
    pagados antes de total_pagado); si no, total_pagado acotado a [0, total]."""
    total = _a_entero(row.get("total", 0))
    if _es_pagado(row.get("pagado", False)):
        return total
    return max(0, min(_a_entero(row.get("total_pagado", 0)), total))


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
            "SELECT id, nombre, precio, activo, orden, agotado_hasta FROM menu ORDER BY orden, id"
        ))
        return pd.DataFrame(resultado.fetchall(), columns=resultado.keys())


# ── Mesas activas (para selectores; F5) ─────────────────────────────────────────
@st.cache_data(ttl=30)
def cargar_mesas_activas():
    """[{id, nombre}] de mesas activas. TTL corto; sin invalidación explícita."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, nombre FROM mesas WHERE activa = TRUE ORDER BY id"
        )).mappings().all()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════════
# OVERHAUL DEL MENÚ — lecturas compartidas (componentes, catálogo, ajustes, clientes)
# Las usan views/menu.py (admin), views/nuevo_pedido.py (POS) y views/monitor_mesas.
# La app pública (app_cliente) tiene su propia conexión y replica lo que necesita.
# ════════════════════════════════════════════════════════════════════════════════

# Grupos de opciones del Plato del Día, en el orden de los pasos de selección.
GRUPOS_COMPONENTE = ["entrada", "principio", "proteina", "acompanamiento"]
GRUPO_LABEL = {
    "entrada":        "Entrada",
    "principio":      "Principio",
    "proteina":       "Carnes o Proteína",
    "acompanamiento": "Acompañamientos",
}


@st.cache_data(ttl=60)
def cargar_componentes():
    """DataFrame de TODOS los componentes del Plato del Día (admin). menu.py llama
    cargar_componentes.clear() tras cada escritura para reflejar los cambios al vuelo."""
    with engine.connect() as conn:
        res = conn.execute(text(
            "SELECT id, grupo, nombre, activo, orden, agotado_hasta "
            "FROM menu_componentes ORDER BY grupo, orden, id"
        ))
        return pd.DataFrame(res.fetchall(), columns=res.keys())


def disponibles(df):
    """Filtra un DataFrame de menú/componentes a lo ofrecible HOY: activo = TRUE y
    no agotado hoy (agotado_hasta NULL o < hoy). Misma regla en menú, componentes y
    carta del cliente (centraliza el filtro que vivía suelto en nuevo_pedido)."""
    if df is None or df.empty:
        return df
    hoy = pd.Timestamp(date.today())
    ag = pd.to_datetime(df["agotado_hasta"], errors="coerce")
    return df[(df["activo"] == True) & (ag.isna() | (ag < hoy))]


def componentes_activos_por_grupo() -> dict:
    """{grupo: [{id, nombre}]} con SOLO los componentes ofrecibles hoy, en orden.
    Lo consume el configurador del Plato del Día (POS y app cliente)."""
    out = {g: [] for g in GRUPOS_COMPONENTE}
    df = cargar_componentes()
    if df.empty:
        return out
    for _, r in disponibles(df).iterrows():
        out.setdefault(r["grupo"], []).append({"id": int(r["id"]), "nombre": str(r["nombre"])})
    return out


@st.cache_data(ttl=60)
def cargar_catalogo():
    """Catálogo 'menu' con categoría y descripción (especiales / a la carta /
    bebidas). Incluye todas las filas; el filtrado por categoría y disponibilidad lo
    hace cada vista. menu.py llama cargar_catalogo.clear() tras cada escritura."""
    with engine.connect() as conn:
        res = conn.execute(text(
            "SELECT id, nombre, precio, activo, orden, agotado_hasta, categoria, descripcion "
            "FROM menu ORDER BY categoria, orden, id"
        ))
        return pd.DataFrame(res.fetchall(), columns=res.keys())


# ── Ajustes: precios planos y recargo de entrega ────────────────────────────────
@st.cache_data(ttl=60)
def cargar_ajustes() -> dict:
    """{clave: valor} de la tabla 'ajustes'. menu.py llama cargar_ajustes.clear()
    tras guardar precios/recargo."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT clave, valor FROM ajustes")).mappings().all()
    return {r["clave"]: r["valor"] for r in rows}


def ajuste_int(clave: str, default: int = 0) -> int:
    """Lee un ajuste como entero (tolerante a vacío/None/no numérico)."""
    try:
        return int(float(cargar_ajustes().get(clave, default)))
    except (TypeError, ValueError):
        return default


def precio_plato_dia() -> int:
    return ajuste_int("plato_dia_precio", 0)


def precio_especiales() -> int:
    return ajuste_int("especiales_precio", 0)


def fee_entrega() -> int:
    return ajuste_int("fee_entrega", 0)


def num_acompanamientos() -> int:
    return max(1, ajuste_int("acompanamientos_n", 3))


# ── Base de clientes (la alimenta la app pública) ───────────────────────────────
def buscar_cliente(telefono: str):
    """{telefono, nombre, direccion} del cliente, o None. Para pre-rellenar el
    formulario de identidad en pedidos repetidos."""
    tel = (telefono or "").strip()[:40]
    if not tel:
        return None
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT telefono, nombre, direccion FROM clientes WHERE telefono = :t"
        ), {"t": tel}).mappings().first()
    return dict(row) if row else None


def upsert_cliente(telefono: str, nombre: str = None, direccion: str = None) -> None:
    """Crea/actualiza el cliente por teléfono. Conserva nombre/dirección previos si
    llegan vacíos (COALESCE con EXCLUDED). Tolerante: nunca rompe el flujo del pedido."""
    tel = (telefono or "").strip()[:40]
    if not tel:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO clientes (telefono, nombre, direccion)
                VALUES (:t, :n, :d)
                ON CONFLICT (telefono) DO UPDATE SET
                    nombre      = COALESCE(EXCLUDED.nombre, clientes.nombre),
                    direccion   = COALESCE(EXCLUDED.direccion, clientes.direccion),
                    actualizado = NOW()
            """), {"t": tel, "n": (nombre or None), "d": (direccion or None)})
    except Exception:
        pass
