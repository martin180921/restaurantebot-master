"""app_cliente: carta digital pública para pedidos a Domicilio / Para Llevar.

Flujo:
  1) Puerta de entrada (bloquea la carta): tipo de entrega (🛵 Domicilio / 🛍️ Para
     Llevar), método de pago (Efectivo → "¿con cuánto pagas?" para calcular el cambio /
     Transferencia) y datos del cliente (nombre, teléfono, y dirección solo si es
     Domicilio). El teléfono construye/actualiza la base de clientes.
  2) Carta dinámica en 4 secciones, idéntica a la del POS:
       #1 Plato del Día — configurable por plato (entrada / principio / proteína /
          N acompañamientos con repetición permitida). Si pides más de uno, se repite
          la configuración para cada plato.
       #2 Especiales — platos con descripción; precio plano de la categoría.
       #3 A la carta — platos sueltos, con un sub-grupo de Bebidas.
       #4 Notas generales — observaciones del pedido al final.
  3) Envío → escribe en `pedidos` (items por secciones + metadatos de entrega y pago)
     y queda listo para que la cocina lo prepare y la caja lo cobre al entregar.

App aislada: su propia conexión; comparte con el panel solo el esquema de la BD.
La URL puede traer ?tel=<num> para identificar y pre-rellenar al cliente.
"""
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import json
import html
import time
import os

load_dotenv()

# Anti-spam del enlace público.
COOLDOWN_SEG          = 25   # espera mínima entre envíos por sesión
MAX_ACTIVAS_POR_TEL   = 5    # tope de pedidos en curso por teléfono

TIPO_LABEL = {"domicilio": "🛵 Domicilio", "para_llevar": "🛍️ Para Llevar"}


def _normalizar_db_url(url):
    """Valida/normaliza DATABASE_URL (C7): 'postgres://' → 'postgresql://'."""
    if not url:
        raise RuntimeError(
            "DATABASE_URL no está configurada. Define la variable de entorno con "
            "la cadena de conexión de PostgreSQL antes de arrancar la app."
        )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


DATABASE_URL = _normalizar_db_url(os.getenv("DATABASE_URL"))
# C5: pre_ping + recycle para no reutilizar conexiones que Railway ya cerró.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)


# ── Esquema defensivo (aditivo) ────────────────────────────────────────────────
# El bot es el dueño canónico del esquema y lo siembra; aquí solo garantizamos que
# las tablas/columnas que esta app LEE y ESCRIBE existan, por si arranca antes del
# redeploy del bot. Idempotente, sin sembrar componentes. Tolerante a fallos.
def _ensure_schema():
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS agotado_hasta DATE"))
            conn.execute(text(
                "ALTER TABLE menu ADD COLUMN IF NOT EXISTS categoria VARCHAR(20) NOT NULL DEFAULT 'a_la_carta'"
            ))
            conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS descripcion TEXT"))
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
            # Inventario diario: stock por componente y por plato (NULL = ilimitado).
            conn.execute(text("ALTER TABLE menu_componentes ADD COLUMN IF NOT EXISTS stock INTEGER"))
            conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS stock INTEGER"))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ajustes (
                    clave VARCHAR(50) PRIMARY KEY,
                    valor TEXT        NOT NULL
                )
            """))
            conn.execute(text("""
                INSERT INTO ajustes (clave, valor) VALUES
                ('plato_dia_precio','18000'),('especiales_precio','25000'),
                ('fee_entrega','4000'),('acompanamientos_n','3')
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
                ("tipo_entrega", "VARCHAR(15)"), ("cliente_nombre", "VARCHAR(120)"),
                ("cliente_telefono", "VARCHAR(40)"), ("direccion", "TEXT"),
                ("metodo_pago", "VARCHAR(20)"), ("paga_con", "INTEGER"),
                ("fee", "INTEGER NOT NULL DEFAULT 0"), ("nota_general", "TEXT"),
            ]:
                conn.execute(text(f"ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS {_col} {_ddl}"))
    except Exception:
        pass


_ensure_schema()


def fmt_money(valor) -> str:
    """35000 → '35.000' (miles con punto, estilo LATAM)."""
    try:
        return f"{int(round(float(valor))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def _int(d: dict, clave: str, default: int = 0) -> int:
    try:
        return int(float(d.get(clave, default)))
    except (TypeError, ValueError):
        return default


def _clean_tel(valor) -> str:
    """Limpia y acota un teléfono que puede venir de la URL (C2)."""
    if not valor:
        return ""
    return "".join(c for c in str(valor) if c not in "<>\"'`").strip()[:40]


# ── Lecturas (cacheadas; la carta cambia poco) ─────────────────────────────────
def _stock_val(v):
    """int del stock o None (ilimitado). El None distingue 'sin control' de '0'."""
    return None if v is None else int(v)


@st.cache_data(ttl=30)
def cargar_componentes_activos() -> dict:
    """{grupo: [{id, nombre, stock}]} de componentes ofrecibles hoy (activo + no agotado).
    Los componentes NO se ocultan por stock 0 (el Plato del Día no se esconde nunca); el
    configurador marca los agotados. 'stock' = porciones restantes o None (ilimitado)."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, grupo, nombre, stock FROM menu_componentes "
            "WHERE activo = TRUE AND (agotado_hasta IS NULL OR agotado_hasta < CURRENT_DATE) "
            "ORDER BY grupo, orden, id"
        )).mappings().all()
    out = {"entrada": [], "principio": [], "proteina": [], "acompanamiento": []}
    for r in rows:
        out.setdefault(r["grupo"], []).append(
            {"id": int(r["id"]), "nombre": r["nombre"], "stock": _stock_val(r["stock"])})
    return out


@st.cache_data(ttl=30)
def cargar_catalogo() -> dict:
    """{categoria: [{id, nombre, precio, descripcion, stock}]} ofrecible hoy. OCULTACIÓN
    ESTRICTA del a la carta: los platos con control de stock en 0 se excluyen aquí mismo
    (stock IS NULL OR stock > 0) → desaparecen de la carta pública."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, nombre, precio, categoria, descripcion, stock FROM menu "
            "WHERE activo = TRUE AND (agotado_hasta IS NULL OR agotado_hasta < CURRENT_DATE) "
            "AND (stock IS NULL OR stock > 0) "
            "ORDER BY categoria, orden, id"
        )).mappings().all()
    out = {"especial": [], "a_la_carta": [], "bebida": []}
    for r in rows:
        out.setdefault(r["categoria"], []).append({
            "id": int(r["id"]), "nombre": r["nombre"],
            "precio": int(r["precio"]), "descripcion": r["descripcion"],
            "stock": _stock_val(r["stock"]),
        })
    return out


@st.cache_data(ttl=30)
def cargar_ajustes() -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT clave, valor FROM ajustes")).mappings().all()
    return {r["clave"]: r["valor"] for r in rows}


# ── Clientes + pedidos ─────────────────────────────────────────────────────────
def buscar_cliente(telefono: str):
    tel = _clean_tel(telefono)
    if not tel:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT telefono, nombre, direccion FROM clientes WHERE telefono = :t"
            ), {"t": tel}).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None


def upsert_cliente(telefono: str, nombre=None, direccion=None):
    tel = _clean_tel(telefono)
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


def _descontar_inventario(conn, items) -> None:
    """Descuenta el stock del pedido en el MISMO txn que el INSERT (atómico). Solo toca
    filas con stock NO NULL (rastreadas): componentes del Plato del Día por (grupo,
    nombre) — entrada/principio/proteína + cada acompañamiento elegido — y los platos a
    la carta por menu_id × cantidad. GREATEST(0, …) evita negativos. No lanza: un fallo
    de inventario no debe tumbar el pedido del cliente."""
    comp_qty, menu_qty = {}, {}
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        cant = int(it.get("cantidad", 1) or 1)
        if cant <= 0:
            continue
        if str(it.get("tipo") or "item").lower() == "plato_dia":
            cfg = it.get("config") or {}
            for g in ("entrada", "principio", "proteina"):
                v = cfg.get(g)
                if v:
                    k = (g, str(v).strip().lower())
                    comp_qty[k] = comp_qty.get(k, 0) + cant
            for a in (cfg.get("acompanamientos") or []):
                if a:
                    k = ("acompanamiento", str(a).strip().lower())
                    comp_qty[k] = comp_qty.get(k, 0) + cant
        else:
            try:
                mid = int(it.get("id"))
            except (TypeError, ValueError):
                continue
            menu_qty[mid] = menu_qty.get(mid, 0) + cant
    try:
        for (grupo, nombre_l), n in comp_qty.items():
            conn.execute(text(
                "UPDATE menu_componentes SET stock = GREATEST(0, stock - :n) "
                "WHERE grupo = :g AND LOWER(nombre) = :nom AND stock IS NOT NULL"
            ), {"n": int(n), "g": grupo, "nom": nombre_l})
        for mid, n in menu_qty.items():
            conn.execute(text(
                "UPDATE menu SET stock = GREATEST(0, stock - :n) WHERE id = :id AND stock IS NOT NULL"
            ), {"n": int(n), "id": int(mid)})
    except Exception:
        pass


def guardar_pedido(numero_cliente, items, total, *, tipo_entrega, cliente_nombre,
                   cliente_telefono, direccion, metodo_pago, paga_con, fee,
                   nota_general) -> int:
    with engine.begin() as conn:
        nuevo_id = int(conn.execute(text("""
            INSERT INTO pedidos
              (numero_cliente, items, total, estado, tipo_entrega, cliente_nombre,
               cliente_telefono, direccion, metodo_pago, paga_con, fee, nota_general)
            VALUES
              (:nc, :items, :total, 'pendiente', :te, :cn, :ct, :dir, :mp, :pc, :fee, :ng)
            RETURNING id
        """), {
            "nc": numero_cliente, "items": json.dumps(items, ensure_ascii=False),
            "total": int(total), "te": tipo_entrega, "cn": cliente_nombre,
            "ct": cliente_telefono, "dir": (direccion or None), "mp": metodo_pago,
            "pc": int(paga_con or 0), "fee": int(fee or 0), "ng": (nota_general or None),
        }).scalar())
        # Descuento inmediato del inventario (mismo txn): evita revender lo ya pedido.
        _descontar_inventario(conn, items)
        return nuevo_id


def estado_pedido(pid: int):
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT estado FROM pedidos WHERE id = :id"),
                               {"id": pid}).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def pedidos_activos_telefono(tel: str) -> int:
    tel = _clean_tel(tel)
    if not tel:
        return 0
    try:
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM pedidos WHERE cliente_telefono = :t "
                "AND estado NOT IN ('entregado', 'cancelado')"
            ), {"t": tel}).scalar()
        return int(n or 0)
    except Exception:
        return 0


# ── Config + estilos (móvil) ───────────────────────────────────────────────────
st.set_page_config(page_title="Carta Digital", page_icon="🍽️",
                   layout="centered", initial_sidebar_state="collapsed")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
.stApp { background: #f7f7f5; color: #1a1a1a; }
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding: 1rem 1rem 7rem 1rem; max-width: 520px; }

.c-header   { text-align: center; padding: 0.5rem 0 0.5rem 0; }
.c-title    { font-size: 1.5rem; font-weight: 700; color: #1a1a1a; }
.c-subtitle { font-size: 0.8rem; color: #999; margin-top: 2px; }
.c-section  { font-size: 0.95rem; font-weight: 700; color: #1a1a1a;
              margin: 1.4rem 0 0.4rem 0; padding-bottom: 4px;
              border-bottom: 2px solid #1a1a1a; }
.c-sub      { font-size: 0.78rem; font-weight: 600; text-transform: uppercase;
              letter-spacing: 1px; color: #999; margin: 0.9rem 0 0.2rem 0; }
.c-name  { font-size: 0.98rem; font-weight: 500; color: #1a1a1a; }
.c-desc  { font-size: 0.8rem; color: #777; font-style: italic; }
.c-price { font-size: 0.85rem; color: #777; }
.c-qty   { text-align: center; font-weight: 700; font-size: 1.05rem;
           padding-top: 10px; color: #1a1a1a; }
.c-empty { text-align: center; color: #aaa; font-size: 0.88rem; padding: 0.8rem 0; }

.plate-card { background: #fff; border: 1px solid #ececec; border-left: 4px solid #1a1a1a;
              border-radius: 12px; padding: 0.8rem 0.9rem; margin: 0.6rem 0; }
.plate-title { font-weight: 700; font-size: 0.95rem; margin-bottom: 0.3rem; }
.conf-label { font-size: 0.78rem; font-weight: 600; color: #555; margin-top: 0.5rem; }
.acc-count  { font-size: 0.75rem; color: #1a1a1a; font-weight: 600; }
.warn { background: #fef3c7; border: 1px solid #fcd34d; color: #92400e;
        border-radius: 8px; padding: 6px 10px; font-size: 0.8rem; margin-top: 4px; }

.stButton > button {
    border-radius: 10px !important; border: 1px solid #e3e3e0 !important;
    background: #fff !important; color: #1a1a1a !important;
    font-weight: 600 !important; font-size: 1.05rem !important;
    padding: 4px 0 !important; width: 100% !important; min-height: 40px;
}
.stButton > button[kind="primary"] {
    background: #1a1a1a !important; color: #fff !important;
    border-color: #1a1a1a !important; font-size: 1rem !important;
    min-height: 54px; border-radius: 14px !important;
}
.c-summary { background: #fff; border: 1px solid #ececec; border-radius: 14px;
             padding: 1rem; margin-bottom: 0.8rem; }
.c-row   { display: flex; justify-content: space-between; font-size: 0.9rem;
           color: #444; padding: 4px 0; gap: 10px; }
.c-row .cfg { color: #888; font-size: 0.78rem; }
.c-fee   { display: flex; justify-content: space-between; font-size: 0.85rem;
           color: #777; padding: 4px 0; border-top: 1px dashed #eee; margin-top: 4px; }
.c-total { display: flex; justify-content: space-between; font-weight: 700;
           font-size: 1.1rem; color: #1a1a1a; border-top: 1px solid #eee;
           margin-top: 6px; padding-top: 8px; }
/* Aviso de pago en efectivo: tarjeta destacada con el saldo a pagar, justo antes
   del botón de envío (solo si el cliente eligió Efectivo en la puerta). */
.cash-card { background: #16a34a; color: #fff; border-radius: 14px;
             padding: 0.9rem 1.1rem; margin: 0.2rem 0 0.9rem 0;
             box-shadow: 0 8px 24px rgba(22,163,74,0.28); }
.cash-card .cc-label { font-size: 0.74rem; color: #dcfce7; font-weight: 600;
             text-transform: uppercase; letter-spacing: 1px; }
.cash-card .cc-total { font-size: 1.9rem; font-weight: 800; line-height: 1.15; margin-top: 2px; }
.cash-card .cc-sub { font-size: 0.88rem; color: #dcfce7; margin-top: 6px; }
.stSelectbox > div > div, .stTextInput > div > div > input,
.stTextArea textarea, .stNumberInput > div > div > input {
    background: #fff !important; border-radius: 10px !important;
}
hr { border-color: #eaeaea !important; }

/* Radios como píldoras (tipo de entrega, método de pago, pasos del plato). */
div[data-testid="stRadio"] > div { gap: 6px !important; }
div[data-testid="stRadio"] label[data-baseweb="radio"] {
    background: #fff; border: 1px solid #e3e3e0; border-radius: 10px;
    padding: 8px 12px; margin: 0 !important;
}

/* CTA primaria fija al fondo (solo hay una por pantalla: gate o envío). */
.stButton:has(button[kind="primary"]) {
    position: fixed; left: 50%; transform: translateX(-50%);
    bottom: 12px; width: 100%; max-width: 520px; padding: 0 1rem; z-index: 1000;
}
.stButton:has(button[kind="primary"]) > button {
    box-shadow: 0 8px 24px rgba(0,0,0,0.22) !important;
}
</style>
""", unsafe_allow_html=True)


# ── Estado de sesión ───────────────────────────────────────────────────────────
st.session_state.setdefault("cart", {})            # {f"{tipo}:{id}": qty}
st.session_state.setdefault("gate", None)          # dict con los datos de la puerta
st.session_state.setdefault("pedido_enviado", None)
st.session_state.setdefault("pd_qty", 0)

qp = st.query_params


# ══════════════════════════════════════════════════════════════════════════════
# Pantalla de seguimiento (tras enviar)
# ══════════════════════════════════════════════════════════════════════════════
def _pantalla_seguimiento():
    info = st.session_state["pedido_enviado"]
    pid = info.get("id")
    estado = estado_pedido(pid) if pid else None
    if estado in ("pendiente", "en preparacion", "listo"):
        st_autorefresh(interval=15000, key="seguimiento_autorefresh")

    st.markdown(f"""
    <div style="text-align:center; padding:1.5rem 1rem 0.5rem 1rem;">
      <div style="font-size:2.4rem;">🧾</div>
      <div style="font-size:1.2rem; font-weight:700; color:#1a1a1a; margin-top:0.4rem;">Pedido #{pid if pid else '—'}</div>
      <div style="color:#777; margin-top:0.2rem;">{html.escape(str(info.get('tipo','')))} · ${fmt_money(info.get('total',0))}</div>
    </div>
    """, unsafe_allow_html=True)

    if estado == "cancelado":
        st.markdown("""
        <div style="text-align:center; padding:1.5rem 1rem;">
          <div style="font-size:2.2rem;">❌</div>
          <div style="font-size:1.05rem; font-weight:700; color:#dc2626; margin-top:0.4rem;">Pedido cancelado</div>
          <div style="color:#999; font-size:0.85rem; margin-top:0.4rem;">Llámanos si crees que es un error.</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        pasos     = ["pendiente", "en preparacion", "listo", "entregado"]
        etiquetas = ["Recibido", "En preparación", "Listo", "¡Entregado!"]
        idx = pasos.index(estado) if estado in pasos else 0
        filas = ""
        for i, et in enumerate(etiquetas):
            if i < idx:
                circ = '<div style="width:28px;height:28px;border-radius:50%;background:#16a34a;color:#fff;display:flex;align-items:center;justify-content:center;font-size:0.85rem;flex:0 0 auto;">✓</div>'
                col, peso = "#16a34a", "500"
            elif i == idx:
                circ = '<div style="width:28px;height:28px;border-radius:50%;background:#16a34a;color:#fff;display:flex;align-items:center;justify-content:center;font-size:0.7rem;flex:0 0 auto;box-shadow:0 0 0 4px #dcfce7;">●</div>'
                col, peso = "#16a34a", "700"
            else:
                circ = '<div style="width:28px;height:28px;border-radius:50%;border:2px solid #ddd;color:#bbb;display:flex;align-items:center;justify-content:center;font-size:0.7rem;flex:0 0 auto;">○</div>'
                col, peso = "#bbb", "500"
            filas += f'<div style="display:flex;align-items:center;gap:12px;">{circ}<span style="color:{col};font-weight:{peso};font-size:1rem;">{et}</span></div>'
            if i < len(etiquetas) - 1:
                conector = "#16a34a" if i < idx else "#eee"
                filas += f'<div style="width:2px;height:14px;background:{conector};margin-left:13px;"></div>'
        st.markdown(f'<div style="max-width:280px; margin:0.5rem auto 1rem auto;">{filas}</div>', unsafe_allow_html=True)
        st.markdown('<div style="text-align:center; color:#999; font-size:0.8rem;">Se actualiza solo cada 15 segundos.</div>', unsafe_allow_html=True)

    if st.button("Hacer otro pedido"):
        st.session_state["pedido_enviado"] = None
        st.session_state["cart"] = {}
        st.session_state["pd_qty"] = 0
        st.rerun()
    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Puerta de entrada (tipo de entrega + pago + datos del cliente)
# ══════════════════════════════════════════════════════════════════════════════
def _pantalla_gate():
    st.markdown("""
    <div class="c-header">
      <div class="c-title">🍽️ Haz tu pedido</div>
      <div class="c-subtitle">Domicilio o para llevar · cuéntanos a dónde va</div>
    </div>
    """, unsafe_allow_html=True)

    tel_param = _clean_tel(qp.get("tel"))
    cli = buscar_cliente(tel_param) if tel_param else None

    st.markdown('<div class="c-sub">Tipo de pedido</div>', unsafe_allow_html=True)
    tipo_lbl = st.radio("Tipo de pedido", ["🛵 Domicilio", "🛍️ Para Llevar"],
                        horizontal=True, label_visibility="collapsed", key="g_tipo")
    es_domicilio = tipo_lbl == "🛵 Domicilio"

    st.markdown('<div class="c-sub">Tus datos</div>', unsafe_allow_html=True)
    nombre = st.text_input("Nombre", value=(cli or {}).get("nombre") or "", key="g_nombre")
    telefono = st.text_input("Número de teléfono",
                             value=tel_param or (cli or {}).get("telefono") or "", key="g_tel")
    direccion = ""
    if es_domicilio:
        direccion = st.text_area("Dirección de entrega",
                                 value=(cli or {}).get("direccion") or "", key="g_dir",
                                 placeholder="Calle, número, barrio, referencias…")

    if st.button("Ver la carta →", type="primary", use_container_width=True):
        errores = []
        if not (nombre or "").strip():
            errores.append("Escribe tu nombre.")
        if len(_clean_tel(telefono)) < 7:
            errores.append("Escribe un teléfono válido.")
        if es_domicilio and not (direccion or "").strip():
            errores.append("La dirección es obligatoria para Domicilio.")
        if errores:
            for e in errores:
                st.warning(e)
        else:
            # El método de pago y el "¿con cuánto pagas?" se piden al FINAL de la carta,
            # ya con el total a la vista (ver _carta), no aquí.
            st.session_state["gate"] = {
                "tipo_entrega": "domicilio" if es_domicilio else "para_llevar",
                "nombre": nombre.strip(),
                "telefono": _clean_tel(telefono),
                "direccion": direccion.strip() if es_domicilio else "",
            }
            st.rerun()
    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Carta (4 secciones) — corre como fragment para reruns locales en los +/-
# ══════════════════════════════════════════════════════════════════════════════
def _disp_suffix(stock) -> str:
    """' (12 disp.)' si lleva control; ' · Agotado' en 0; '' si es ilimitado (None)."""
    if stock is None:
        return ""
    return f" ({int(stock)} disp.)" if int(stock) > 0 else " · Agotado"


def _agotado(opcion) -> bool:
    """True si un componente lleva control de stock y está en 0."""
    s = opcion.get("stock")
    return s is not None and int(s) <= 0


def _sanea_radio(key: str, opciones) -> None:
    """Si el valor guardado de un radio ya no está entre las opciones (p. ej. su opción
    se agotó), lo limpia para que Streamlit no reviente con 'default not in options'."""
    if key in st.session_state and st.session_state[key] not in opciones:
        del st.session_state[key]


def _stepper(key: str, qty: int, *, permitir_mas=True):
    """Fila −/cant/+ reutilizable. Devuelve la nueva cantidad (mutando session)."""
    c_minus, c_qty, c_plus = st.columns([1, 1, 1])
    with c_minus:
        if st.button("−", key=f"minus_{key}"):
            if qty > 0:
                st.session_state["cart"][key] = qty - 1
                if st.session_state["cart"][key] == 0:
                    del st.session_state["cart"][key]
            st.rerun(scope="fragment")
    with c_qty:
        st.markdown(f'<div class="c-qty">{qty}</div>', unsafe_allow_html=True)
    with c_plus:
        if st.button("+", key=f"plus_{key}", disabled=not permitir_mas):
            st.session_state["cart"][key] = qty + 1
            st.rerun(scope="fragment")


def _seccion_catalogo(productos, tipo, con_desc=False):
    """Renderiza una lista de productos con stepper y devuelve los items elegidos."""
    if not productos:
        st.markdown('<div class="c-empty">No disponible por ahora.</div>', unsafe_allow_html=True)
        return []
    carrito = st.session_state["cart"]
    elegidos = []
    for p in productos:
        pid = int(p["id"])
        key = f"{tipo}:{pid}"
        qty = carrito.get(key, 0)
        c_info, c_step = st.columns([3, 2])
        with c_info:
            desc = (f'<div class="c-desc">{html.escape(str(p.get("descripcion") or ""))}</div>'
                    if con_desc and p.get("descripcion") else "")
            st.markdown(
                f'<div style="padding:8px 0;"><span class="c-name">{html.escape(str(p["nombre"]))}</span>'
                f'{desc}<div class="c-price">${fmt_money(p["precio"])}{_disp_suffix(p.get("stock"))}</div></div>',
                unsafe_allow_html=True,
            )
        with c_step:
            # No se puede pedir más de lo que queda (si el plato lleva control de stock).
            tope = (p.get("stock") is not None and qty >= int(p["stock"]))
            _stepper(key, qty, permitir_mas=not tope)
        if qty > 0:
            elegidos.append({"tipo": tipo, "id": pid, "nombre": p["nombre"],
                             "precio": int(p["precio"]), "cantidad": qty})
    return elegidos


def _seccion_plato_dia(comp, precio, n):
    """Configurador del Plato del Día. Devuelve (items, ok) — ok=False si algún plato
    no tiene exactamente n acompañamientos."""
    st.markdown('<div class="c-section">🍛 Plato del Día</div>', unsafe_allow_html=True)
    faltan = [g for g in ("entrada", "principio", "proteina", "acompanamiento") if not comp.get(g)]
    if faltan:
        st.markdown('<div class="c-empty">El Plato del Día no está disponible hoy.</div>',
                    unsafe_allow_html=True)
        return [], True

    # Opciones DISPONIBLES (excluye las agotadas: stock 0). Si un grupo obligatorio queda
    # sin opciones, no hay combinación válida → Plato del Día no disponible por ahora.
    disp_ent = [e for e in comp["entrada"] if not _agotado(e)]
    disp_pri = [p for p in comp["principio"] if not _agotado(p)]
    disp_pro = [p for p in comp["proteina"] if not _agotado(p)]
    if not (disp_ent and disp_pri and disp_pro):
        st.markdown('<div class="c-empty">El Plato del Día no está disponible por ahora '
                    '(algún ingrediente se agotó).</div>', unsafe_allow_html=True)
        return [], True

    st.markdown(f'<div class="c-price">${fmt_money(precio)} cada uno · elige {n} acompañamientos '
                '(puedes repetir)</div>', unsafe_allow_html=True)

    qty = int(st.session_state.get("pd_qty", 0))
    c_lbl, c_step = st.columns([3, 2])
    with c_lbl:
        st.markdown('<div style="padding:8px 0;" class="c-name">¿Cuántos platos del día?</div>',
                    unsafe_allow_html=True)
    with c_step:
        cm, cq, cp = st.columns([1, 1, 1])
        with cm:
            if st.button("−", key="pd_qty_minus"):
                st.session_state["pd_qty"] = max(0, qty - 1)
                st.rerun(scope="fragment")
        with cq:
            st.markdown(f'<div class="c-qty">{qty}</div>', unsafe_allow_html=True)
        with cp:
            if st.button("+", key="pd_qty_plus"):
                st.session_state["pd_qty"] = qty + 1
                st.rerun(scope="fragment")

    acomps = comp["acompanamiento"]
    # Mapas nombre→stock para mostrar las porciones restantes junto a cada opción.
    stock_ent = {e["nombre"]: e.get("stock") for e in disp_ent}
    stock_pri = {p["nombre"]: p.get("stock") for p in disp_pri}
    stock_pro = {p["nombre"]: p.get("stock") for p in disp_pro}
    nom_ent = [e["nombre"] for e in disp_ent]
    nom_pri = [p["nombre"] for p in disp_pri]
    nom_pro = [p["nombre"] for p in disp_pro]

    plates, ok = [], True
    for i in range(qty):
        st.markdown(f'<div class="plate-card"><div class="plate-title">Plato #{i+1}</div></div>',
                    unsafe_allow_html=True)
        # Sanea la selección guardada si su opción se agotó (evita el crash de Streamlit).
        _sanea_radio(f"pd_{i}_entrada", nom_ent)
        _sanea_radio(f"pd_{i}_principio", nom_pri)
        _sanea_radio(f"pd_{i}_proteina", nom_pro)
        st.markdown('<div class="conf-label">Entrada</div>', unsafe_allow_html=True)
        entrada = st.radio("Entrada", nom_ent, key=f"pd_{i}_entrada",
                           format_func=lambda nm: f"{nm}{_disp_suffix(stock_ent.get(nm))}",
                           label_visibility="collapsed")
        st.markdown('<div class="conf-label">Principio</div>', unsafe_allow_html=True)
        principio = st.radio("Principio", nom_pri, key=f"pd_{i}_principio",
                             format_func=lambda nm: f"{nm}{_disp_suffix(stock_pri.get(nm))}",
                             label_visibility="collapsed")
        st.markdown('<div class="conf-label">Carnes o Proteína</div>', unsafe_allow_html=True)
        proteina = st.radio("Proteína", nom_pro, key=f"pd_{i}_proteina",
                            format_func=lambda nm: f"{nm}{_disp_suffix(stock_pro.get(nm))}",
                            label_visibility="collapsed")

        cuentas = st.session_state.setdefault(f"pd_{i}_acomp", {})
        elegidos_n = sum(cuentas.values())
        st.markdown(f'<div class="conf-label">Acompañamientos '
                    f'<span class="acc-count">({elegidos_n}/{n})</span></div>', unsafe_allow_html=True)
        for a in acomps:
            aid = str(a["id"])
            stock_a = a.get("stock")
            agot = _agotado(a)
            c = int(cuentas.get(aid, 0))
            c_an, c_as = st.columns([3, 2])
            with c_an:
                color = "#aaa" if agot else "#1a1a1a"
                st.markdown(f'<div style="padding:6px 0; color:{color};" class="c-name">'
                            f'{html.escape(str(a["nombre"]))}{_disp_suffix(stock_a)}</div>',
                            unsafe_allow_html=True)
            with c_as:
                cm2, cq2, cp2 = st.columns([1, 1, 1])
                with cm2:
                    if st.button("−", key=f"pd_{i}_acm_{aid}"):
                        if c > 0:
                            cuentas[aid] = c - 1
                            if cuentas[aid] == 0:
                                del cuentas[aid]
                        st.rerun(scope="fragment")
                with cq2:
                    st.markdown(f'<div class="c-qty">{c}</div>', unsafe_allow_html=True)
                with cp2:
                    tope_stock = (stock_a is not None and c >= int(stock_a))
                    if st.button("+", key=f"pd_{i}_acp_{aid}",
                                 disabled=(elegidos_n >= n or agot or tope_stock)):
                        cuentas[aid] = c + 1
                        st.rerun(scope="fragment")

        nota = st.text_input("Nota para este plato (opcional)", key=f"pd_{i}_nota",
                             placeholder="Ej: sin cebolla")

        acomp_list = []
        for a in acomps:
            acomp_list += [a["nombre"]] * int(cuentas.get(str(a["id"]), 0))
        if elegidos_n != n:
            ok = False
            st.markdown(f'<div class="warn">Elige exactamente {n} acompañamientos para el Plato #{i+1}.</div>',
                        unsafe_allow_html=True)

        plates.append({
            "tipo": "plato_dia", "nombre": "Plato del Día", "precio": int(precio),
            "cantidad": 1,
            "config": {"entrada": entrada, "principio": principio, "proteina": proteina,
                       "acompanamientos": acomp_list},
            "nota": (nota or "").strip(),
        })
    return plates, ok


def _resumen_item_cfg(it) -> str:
    """Texto pequeño con la configuración de un plato del día para el resumen."""
    cfg = it.get("config") or {}
    partes = [cfg.get("entrada"), cfg.get("principio"), cfg.get("proteina")]
    ac = cfg.get("acompanamientos") or []
    if ac:
        # colapsa duplicados: 2x Arroz, 1x Maduro
        orden, cnt = [], {}
        for a in ac:
            if a not in cnt:
                orden.append(a)
            cnt[a] = cnt.get(a, 0) + 1
        partes.append(", ".join(f"{cnt[a]}x {a}" for a in orden))
    txt = " · ".join(p for p in partes if p)
    if it.get("nota"):
        txt += f" · Nota: {it['nota']}"
    return txt


@st.fragment
def _carta(comp, cat, ajustes):
    # Datos ya cargados fuera del fragment (bajo el guard de errores): así los reruns
    # de los +/- nunca tocan la BD y la carta queda estable durante el pedido.
    gate = st.session_state["gate"]
    fee = _int(ajustes, "fee_entrega", 0)
    pd_precio = _int(ajustes, "plato_dia_precio", 0)
    n_ac = max(1, _int(ajustes, "acompanamientos_n", 3))

    st.markdown(
        f'<div class="c-header"><div class="c-title">🍽️ Nuestra carta</div>'
        f'<div class="c-subtitle">{TIPO_LABEL.get(gate["tipo_entrega"], "")} · '
        f'{html.escape(gate["nombre"])}</div></div>',
        unsafe_allow_html=True,
    )

    # #1 Plato del Día
    items_pd, ok_pd = _seccion_plato_dia(comp, pd_precio, n_ac)

    # #2 Especiales
    st.markdown('<div class="c-section">⭐ Especiales</div>', unsafe_allow_html=True)
    items_esp = _seccion_catalogo(cat.get("especial", []), "especial", con_desc=True)

    # #3 A la carta (+ sub-grupo Bebidas)
    st.markdown('<div class="c-section">🍽️ A la carta</div>', unsafe_allow_html=True)
    items_alc = _seccion_catalogo(cat.get("a_la_carta", []), "item")
    st.markdown('<div class="c-sub">🥤 Bebidas</div>', unsafe_allow_html=True)
    items_beb = _seccion_catalogo(cat.get("bebida", []), "bebida")

    items = items_pd + items_esp + items_alc + items_beb

    # #4 Notas generales + resumen + envío
    st.markdown('<div class="c-section">📝 Notas generales</div>', unsafe_allow_html=True)
    notas = st.text_area("Notas generales", key="notas_generales", label_visibility="collapsed",
                         placeholder="Observaciones para todo el pedido (timbre dañado, sin cubiertos…)")

    st.markdown('<div class="c-section">🧾 Tu pedido</div>', unsafe_allow_html=True)
    if not items:
        st.markdown('<div class="c-empty">Agrega platos para armar tu pedido.</div>',
                    unsafe_allow_html=True)
        return

    subtotal = sum(int(it["precio"]) * int(it["cantidad"]) for it in items)
    total = subtotal + fee

    filas = ""
    for it in items:
        if it.get("tipo") == "plato_dia":
            filas += (f'<div class="c-row"><span>1× Plato del Día'
                      f'<div class="cfg">{html.escape(_resumen_item_cfg(it))}</div></span>'
                      f'<span>${fmt_money(it["precio"])}</span></div>')
        else:
            filas += (f'<div class="c-row"><span>{it["cantidad"]}× {html.escape(str(it["nombre"]))}</span>'
                      f'<span>${fmt_money(int(it["precio"]) * int(it["cantidad"]))}</span></div>')
    fee_lbl = "Domicilio" if gate["tipo_entrega"] == "domicilio" else "Para llevar"
    st.markdown(
        f'<div class="c-summary">{filas}'
        f'<div class="c-fee"><span>Recargo · {fee_lbl}</span><span>${fmt_money(fee)}</span></div>'
        f'<div class="c-total"><span>Total</span><span>${fmt_money(total)}</span></div></div>',
        unsafe_allow_html=True,
    )

    if not ok_pd:
        st.markdown('<div class="warn">Completa los acompañamientos de cada Plato del Día '
                    'antes de enviar.</div>', unsafe_allow_html=True)

    # Pago AL FINAL, ya con el total a la vista: el cliente elige método y, si es efectivo,
    # indica con cuánto paga y ve su cambio antes de enviar. (Antes esto se preguntaba en la
    # puerta de entrada, sin que supiera todavía el valor de su pedido.)
    st.markdown('<div class="c-section">💳 ¿Cómo vas a pagar?</div>', unsafe_allow_html=True)
    metodo_lbl = st.radio("Método de pago", ["💵 Efectivo", "💳 Transferencia"],
                          horizontal=True, label_visibility="collapsed", key="c_metodo")
    es_efectivo = metodo_lbl == "💵 Efectivo"
    metodo_pago = "efectivo" if es_efectivo else "transferencia"
    paga_con = 0
    if es_efectivo:
        paga_con = int(st.number_input("¿Con cuánto vas a pagar? (para tu cambio)",
                                       min_value=0, step=1000, key="c_paga_con") or 0)
        if paga_con >= total and paga_con > 0:
            cc_sub = (f'<div class="cc-sub">Pagas con ${fmt_money(paga_con)} · '
                      f'tu cambio: ${fmt_money(paga_con - total)}</div>')
        elif paga_con > 0:
            cc_sub = (f'<div class="cc-sub">Te faltan ${fmt_money(total - paga_con)} · '
                      f'ten listo al menos ${fmt_money(total)}</div>')
        else:
            cc_sub = '<div class="cc-sub">Escribe con cuánto pagas para ver tu cambio</div>'
        st.markdown(
            f'<div class="cash-card"><div class="cc-label">💵 Pago en efectivo · Total a pagar</div>'
            f'<div class="cc-total">${fmt_money(total)}</div>{cc_sub}</div>',
            unsafe_allow_html=True,
        )

    falta_efectivo = es_efectivo and paga_con <= 0
    if st.button(f"Enviar pedido · ${fmt_money(total)}", type="primary",
                 use_container_width=True, disabled=(not ok_pd or falta_efectivo)):
        ahora = time.time()
        if ahora - st.session_state.get("ultimo_envio", 0) < COOLDOWN_SEG:
            st.toast("Espera unos segundos antes de enviar otro pedido.", icon="⏳")
        elif pedidos_activos_telefono(gate["telefono"]) >= MAX_ACTIVAS_POR_TEL:
            st.toast("Ya tienes varios pedidos en curso. Llámanos para otro.", icon="🚦")
        else:
            nuevo_id = guardar_pedido(
                gate["nombre"], items, total,
                tipo_entrega=gate["tipo_entrega"], cliente_nombre=gate["nombre"],
                cliente_telefono=gate["telefono"], direccion=gate["direccion"],
                metodo_pago=metodo_pago, paga_con=paga_con,
                fee=fee, nota_general=(notas or "").strip(),
            )
            upsert_cliente(gate["telefono"], gate["nombre"], gate["direccion"] or None)
            st.session_state["ultimo_envio"] = ahora
            st.session_state["pedido_enviado"] = {
                "id": nuevo_id, "tipo": TIPO_LABEL.get(gate["tipo_entrega"], ""), "total": total,
            }
            # Limpia la carta y la configuración de platos del día.
            st.session_state["cart"] = {}
            for k in [k for k in st.session_state if str(k).startswith("pd_")]:
                del st.session_state[k]
            st.session_state.pop("notas_generales", None)
            st.session_state.pop("c_metodo", None)
            st.session_state.pop("c_paga_con", None)
            st.session_state["pd_qty"] = 0
            st.rerun(scope="app")


# ══════════════════════════════════════════════════════════════════════════════
# Flujo principal
# ══════════════════════════════════════════════════════════════════════════════
if st.session_state["pedido_enviado"]:
    _pantalla_seguimiento()

if st.session_state["gate"] is None:
    _pantalla_gate()

# Carga de la carta a prueba de fallos para el cliente (fuera del fragment).
try:
    _comp = cargar_componentes_activos()
    _cat = cargar_catalogo()
    _ajustes = cargar_ajustes()
except Exception:
    st.error("El servicio no está disponible en este momento. Intenta más tarde.")
    st.stop()

_carta(_comp, _cat, _ajustes)
