"""app_cliente: carta digital móvil.

El comensal abre el enlace (que el bot de WhatsApp le envía), elige su mesa,
arma el carrito y envía el pedido directo a la base de datos. La cocina lo ve
aparecer en el panel en su próximo refresco. Sin sidebars, optimizado para móvil.

La URL puede traer parámetros del enlace del bot:
    ?mesa=<id>   → preselecciona la mesa
    ?tel=<num>   → identifica al cliente en el pedido
"""
import streamlit as st
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import json
import html
import os

load_dotenv()


def _normalizar_db_url(url):
    """Valida/normaliza DATABASE_URL (C7).

    Railway entrega 'postgres://', que SQLAlchemy 2.0 rechaza → 'postgresql://'.
    Si falta, fallamos con un mensaje claro en vez de create_engine(None).
    """
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


# ── Formato de moneda LATAM $XX.XXX (C6) ────────────────────────────────────────
def fmt_money(valor) -> str:
    """Formatea un monto entero con punto de miles: 35000 → '35.000'."""
    try:
        return f"{int(round(float(valor))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"

st.set_page_config(
    page_title="Carta Digital",
    page_icon="🍽️",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ── Estilos: limpio, claro, optimizado para móvil ──────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
.stApp { background: #f7f7f5; color: #1a1a1a; }
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding: 1rem 1rem 4rem 1rem; max-width: 520px; }

.c-header   { text-align: center; padding: 0.5rem 0 1rem 0; }
.c-title    { font-size: 1.5rem; font-weight: 700; color: #1a1a1a; }
.c-subtitle { font-size: 0.8rem; color: #999; margin-top: 2px; }
.c-section  { font-size: 0.78rem; font-weight: 600; text-transform: uppercase;
              letter-spacing: 1px; color: #999; margin: 1.2rem 0 0.2rem 0; }

.c-item  { display: flex; flex-direction: column; padding: 10px 0; }
.c-name  { font-size: 0.98rem; font-weight: 500; color: #1a1a1a; }
.c-price { font-size: 0.85rem; color: #777; }
.c-qty   { text-align: center; font-weight: 700; font-size: 1.05rem;
           padding-top: 12px; color: #1a1a1a; }

.stButton > button {
    border-radius: 10px !important; border: 1px solid #e3e3e0 !important;
    background: #fff !important; color: #1a1a1a !important;
    font-weight: 600 !important; font-size: 1.15rem !important;
    padding: 4px 0 !important; width: 100% !important; min-height: 42px;
}
.stButton > button[kind="primary"] {
    background: #1a1a1a !important; color: #fff !important;
    border-color: #1a1a1a !important; font-size: 1rem !important;
    min-height: 54px; border-radius: 14px !important;
}
.c-summary { background: #fff; border: 1px solid #ececec; border-radius: 14px;
             padding: 1rem; margin-bottom: 0.8rem; }
.c-row   { display: flex; justify-content: space-between; font-size: 0.9rem;
           color: #444; padding: 4px 0; }
.c-total { display: flex; justify-content: space-between; font-weight: 700;
           font-size: 1.1rem; color: #1a1a1a; border-top: 1px solid #eee;
           margin-top: 6px; padding-top: 8px; }
.c-empty { text-align: center; color: #aaa; font-size: 0.9rem; padding: 1rem 0; }
.stSelectbox > div > div { background: #fff !important; border-radius: 10px !important; }
hr { border-color: #eaeaea !important; }
</style>
""", unsafe_allow_html=True)


# ── DB ─────────────────────────────────────────────────────────────────────────
# P1: menú y mesas cambian poco; cacheamos con TTL corto para no consultar la BD
# en cada tap de +/-. Devolvemos dicts (no RowMapping) por serializabilidad.
@st.cache_data(ttl=30)
def cargar_menu():
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, nombre, precio FROM menu WHERE activo = TRUE ORDER BY orden, id"
        )).mappings().all()
    return [dict(r) for r in rows]

@st.cache_data(ttl=30)
def cargar_mesas():
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, nombre FROM mesas WHERE activa = TRUE ORDER BY id"
        )).mappings().all()
    return [dict(r) for r in rows]

def guardar_pedido(numero_cliente: str, mesa_id: int, items: list, total: int):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pedidos (numero_cliente, items, total, estado, mesa_id)
            VALUES (:n, :i, :t, 'pendiente', :m)
        """), {"n": numero_cliente, "i": json.dumps(items, ensure_ascii=False),
               "t": total, "m": mesa_id})


# ── Pedido (fragment P4) ────────────────────────────────────────────────────────
# Menú + resumen + envío corren como un fragment: los +/- relanzan SOLO este
# bloque (st.rerun(scope="fragment")) en vez de re-ejecutar todo el script.
@st.fragment
def _ordenar(menu, mesa_sel, mesa_labels, qp):
    st.markdown('<div class="c-section">Menú</div>', unsafe_allow_html=True)
    carrito = st.session_state["carrito"]

    for item in menu:
        iid    = int(item["id"])
        nombre = item["nombre"]
        precio = int(item["precio"])
        qty    = carrito.get(iid, 0)

        c_info, c_minus, c_qty, c_plus = st.columns([4, 1, 1, 1])
        with c_info:
            st.markdown(
                f'<div class="c-item"><span class="c-name">{html.escape(str(nombre))}</span>'
                f'<span class="c-price">${fmt_money(precio)}</span></div>',
                unsafe_allow_html=True,
            )
        with c_minus:
            if st.button("−", key=f"m_{iid}"):
                if qty > 0:
                    carrito[iid] = qty - 1
                    if carrito[iid] == 0:
                        del carrito[iid]
                st.rerun(scope="fragment")
        with c_qty:
            st.markdown(f'<div class="c-qty">{qty}</div>', unsafe_allow_html=True)
        with c_plus:
            if st.button("+", key=f"p_{iid}"):
                carrito[iid] = qty + 1
                st.rerun(scope="fragment")

    menu_by_id   = {int(m["id"]): m for m in menu}
    items_pedido = []
    total        = 0
    for iid, qty in carrito.items():
        m = menu_by_id.get(iid)
        if m and qty > 0:
            precio   = int(m["precio"])
            subtotal = precio * qty
            total   += subtotal
            items_pedido.append({"id": str(iid), "nombre": m["nombre"],
                                 "precio": precio, "cantidad": qty})

    st.markdown("<hr>", unsafe_allow_html=True)

    if not items_pedido:
        st.markdown('<div class="c-empty">Agrega platos para armar tu pedido.</div>',
                    unsafe_allow_html=True)
        return

    filas = "".join(
        f'<div class="c-row"><span>{it["cantidad"]}× {html.escape(str(it["nombre"]))}</span>'
        f'<span>${fmt_money(it["precio"] * it["cantidad"])}</span></div>'
        for it in items_pedido
    )
    st.markdown(
        f'<div class="c-summary">{filas}'
        f'<div class="c-total"><span>Total</span><span>${fmt_money(total)}</span></div></div>',
        unsafe_allow_html=True,
    )

    if st.button(f"Enviar pedido · ${fmt_money(total)}", type="primary", use_container_width=True):
        # C2: ?tel viene de una URL pública. Lo limpiamos y acotamos antes de
        # guardarlo (y además se escapa al renderizarlo en el panel/ticket).
        tel_param = qp.get("tel")
        if tel_param:
            limpio = "".join(c for c in str(tel_param) if c not in "<>\"'`").strip()[:40]
            numero_cliente = limpio or mesa_labels[mesa_sel]
        else:
            numero_cliente = mesa_labels[mesa_sel]
        guardar_pedido(numero_cliente, mesa_sel, items_pedido, total)
        st.session_state["pedido_enviado"] = {"mesa": mesa_labels[mesa_sel], "total": total}
        st.session_state["carrito"] = {}
        st.rerun(scope="app")  # salir del fragment → pantalla de confirmación


# ── Estado de sesión ───────────────────────────────────────────────────────────
if "carrito" not in st.session_state:
    st.session_state["carrito"] = {}
if "pedido_enviado" not in st.session_state:
    st.session_state["pedido_enviado"] = None


# ── Pantalla de confirmación ───────────────────────────────────────────────────
if st.session_state["pedido_enviado"]:
    info = st.session_state["pedido_enviado"]
    st.markdown(f"""
    <div style="text-align:center; padding:3rem 1rem;">
      <div style="font-size:3rem;">✅</div>
      <div style="font-size:1.3rem; font-weight:700; color:#1a1a1a; margin-top:0.5rem;">¡Pedido enviado!</div>
      <div style="color:#777; margin-top:0.4rem;">{html.escape(str(info['mesa']))} · ${fmt_money(info['total'])}</div>
      <div style="color:#999; font-size:0.85rem; margin-top:0.9rem;">La cocina ya recibió tu pedido. ¡Gracias!</div>
    </div>
    """, unsafe_allow_html=True)
    if st.button("Hacer otro pedido"):
        st.session_state["pedido_enviado"] = None
        st.session_state["carrito"] = {}
        st.rerun()
    st.stop()


# ── Carga de datos (a prueba de fallos para el comensal) ───────────────────────
try:
    menu  = cargar_menu()
    mesas = cargar_mesas()
except Exception:
    st.error("El servicio no está disponible en este momento. Intenta más tarde.")
    st.stop()

st.markdown("""
<div class="c-header">
  <div class="c-title">🍽️ Carta Digital</div>
  <div class="c-subtitle">Arma tu pedido y envíalo a la cocina</div>
</div>
""", unsafe_allow_html=True)

if not mesas:
    st.warning("El restaurante aún no tiene mesas disponibles. Avisa al personal.")
    st.stop()
if not menu:
    st.warning("El menú no está disponible en este momento.")
    st.stop()


# ── Selección de mesa (con soporte de ?mesa=<id> en la URL) ────────────────────
mesa_ids    = [int(m["id"]) for m in mesas]
mesa_labels = {int(m["id"]): m["nombre"] for m in mesas}

qp         = st.query_params
mesa_param = qp.get("mesa")
default_idx = 0
if mesa_param and str(mesa_param).isdigit() and int(mesa_param) in mesa_ids:
    default_idx = mesa_ids.index(int(mesa_param))

st.markdown('<div class="c-section">Tu mesa</div>', unsafe_allow_html=True)
mesa_sel = st.selectbox(
    "Tu mesa", options=mesa_ids, index=default_idx,
    format_func=lambda i: mesa_labels[i], label_visibility="collapsed",
)


# ── Menú + resumen + envío (corre como fragment, P4) ───────────────────────────
_ordenar(menu, mesa_sel, mesa_labels, qp)
