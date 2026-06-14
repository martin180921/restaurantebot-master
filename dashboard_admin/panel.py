"""Enrutador principal del panel: login, estilos globales y navegación.

Cada pestaña vive en su propio módulo dentro de views/ para poder trabajarlas
de forma independiente:
    - views/pedidos.py       → tablero, alertas de audio y tickets
    - views/nuevo_pedido.py  → creación manual de pedidos
    - views/menu.py          → CRUD del menú
"""
import streamlit as st
import streamlit.components.v1
from dotenv import load_dotenv
import hashlib
import os
from datetime import datetime

from views import pedidos, nuevo_pedido, menu, mesas

load_dotenv()

PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")

# ── Config ─────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="RestauranteBOT · Panel",
    page_icon="🍽️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ── Login ──────────────────────────────────────────────────────────────────────
# Auth persists via st.query_params so auto-refresh doesn't log out the user.
def _auth_token():
    return hashlib.sha256(PANEL_PASSWORD.encode()).hexdigest()[:16]

params = st.query_params
if params.get("auth") == _auth_token():
    st.session_state["autenticado"] = True

if "autenticado" not in st.session_state:
    st.session_state["autenticado"] = False

if not st.session_state["autenticado"]:
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500&display=swap');
    html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
    .stApp { background: #0f0f0f; color: #f0ede8; }
    #MainMenu, footer, header { visibility: hidden; }
    .stTextInput > div > div > input {
        background: #1a1a1a !important; border-color: #333 !important;
        color: #f0ede8 !important; border-radius: 8px !important;
        text-align: center; font-size: 1.1rem; letter-spacing: 4px;
    }
    .stButton > button {
        width: 100%; background: #f0ede8 !important; color: #0f0f0f !important;
        border: none !important; border-radius: 8px !important;
        font-family: 'DM Sans', sans-serif !important;
        font-weight: 600 !important; font-size: 0.9rem !important;
        padding: 10px !important; margin-top: 8px !important;
    }
    .stButton > button:hover { background: #ddd9d4 !important; }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("<div style='height: 8vh'></div>", unsafe_allow_html=True)
    col_c, col_i, col_c2 = st.columns([1, 2, 1])
    with col_i:
        st.markdown("""
        <div style='text-align:center; margin-bottom: 2rem;'>
          <div style='font-family:Syne,sans-serif; font-size:1.5rem; font-weight:800; color:#f0ede8;'>🍽️ RestauranteBOT</div>
          <div style='font-size:0.82rem; color:#555; margin-top:4px;'>Panel de operaciones · Acceso restringido</div>
        </div>
        """, unsafe_allow_html=True)
        password_input = st.text_input(
            "Contraseña", type="password", placeholder="••••••••",
            label_visibility="collapsed", key="login_input"
        )
        if st.button("Entrar", key="btn_login"):
            if password_input == PANEL_PASSWORD:
                st.session_state["autenticado"] = True
                st.query_params["auth"] = _auth_token()
                st.rerun()
            else:
                st.error("Contraseña incorrecta")
    st.stop()

# ── Auto-refresh (30s) ────────────────────────────────────────────────────────
# JS-only refresh: reloads the Streamlit iframe every 30s.
# Does NOT call st.rerun() so session_state["autenticado"] is preserved.
st.components.v1.html(
    "<script>setTimeout(function(){ window.parent.location.reload(); }, 30000);</script>",
    height=0
)

# ── Estilos ────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:wght@300;400;500&display=swap');

html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
.stApp { background: #0f0f0f; color: #f0ede8; }

.panel-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 2rem 0 1.5rem 0; border-bottom: 1px solid #222; margin-bottom: 2rem;
}
.panel-title {
    font-family: 'Syne', sans-serif; font-size: 1.6rem;
    font-weight: 800; color: #f0ede8; letter-spacing: -0.5px;
}
.panel-subtitle { font-size: 0.8rem; color: #666; margin-top: 2px; }
.live-dot {
    width: 8px; height: 8px; background: #22c55e; border-radius: 50%;
    display: inline-block; margin-right: 6px; animation: pulse 1.8s infinite;
}
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

.metric-card {
    background: #161616; border: 1px solid #222;
    border-radius: 12px; padding: 1.2rem 1.5rem; text-align: center;
}
.metric-value {
    font-family: 'Syne', sans-serif; font-size: 2.4rem;
    font-weight: 800; color: #f0ede8; line-height: 1;
}
.metric-label {
    font-size: 0.72rem; color: #555;
    text-transform: uppercase; letter-spacing: 1px; margin-top: 4px;
}
.metric-accent { color: #f59e0b; }
.metric-green  { color: #22c55e; }
.metric-blue   { color: #60a5fa; }

.badge {
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 0.72rem; font-weight: 500; letter-spacing: 0.3px;
}
.badge-pendiente   { background: #292218; color: #f59e0b; border: 1px solid #3d2e10; }
.badge-preparacion { background: #1a1f35; color: #60a5fa; border: 1px solid #1e3a5f; }
.badge-listo       { background: #1a2e22; color: #4ade80; border: 1px solid #166534; }
.badge-entregado   { background: #1a1a1a; color: #888;    border: 1px solid #333; }
.badge-cancelado   { background: #1a0a0a; color: #f87171; border: 1px solid #7f1d1d; }
.badge-activo      { background: #1a2e22; color: #4ade80; border: 1px solid #166534; }
.badge-inactivo    { background: #1a1a1a; color: #555;    border: 1px solid #333; }

.order-card {
    background: #141414; border: 1px solid #222; border-radius: 14px;
    padding: 1.2rem 1.4rem; margin-bottom: 0.8rem; transition: border-color 0.2s;
}
.order-card:hover { border-color: #333; }
.order-id    { font-family: 'Syne', sans-serif; font-size: 0.75rem; color: #444; }
.order-num   { font-size: 0.9rem; font-weight: 500; color: #f0ede8; }
.order-items { font-size: 0.82rem; color: #888; margin: 4px 0; }
.order-total { font-family: 'Syne', sans-serif; font-size: 1.1rem; font-weight: 700; color: #f0ede8; }
.order-fecha { font-size: 0.72rem; color: #444; }

.menu-card {
    background: #141414; border: 1px solid #222; border-radius: 14px;
    padding: 1rem 1.2rem; margin-bottom: 0.6rem; transition: border-color 0.2s;
}
.menu-card:hover { border-color: #333; }
.menu-card.inactivo { opacity: 0.45; }
.menu-nombre { font-family: 'Syne', sans-serif; font-size: 1rem; font-weight: 700; color: #f0ede8; }
.menu-precio { font-size: 0.85rem; color: #888; margin-top: 2px; }

.section-title {
    font-family: 'Syne', sans-serif; font-size: 1rem;
    font-weight: 700; color: #f0ede8; margin-bottom: 1rem;
    padding-bottom: 0.5rem; border-bottom: 1px solid #1e1e1e;
}

/* Fix 2: All buttons base */
.stButton > button {
    border-radius: 8px !important; font-family: 'DM Sans', sans-serif !important;
    font-size: 0.78rem !important; font-weight: 500 !important;
    border: 1px solid #333 !important; background: #1a1a1a !important;
    color: #aaa !important; padding: 6px 12px !important;
    transition: all 0.15s !important; height: auto !important;
    width: 100% !important;
}
.stButton > button:hover {
    background: #222 !important; color: #f0ede8 !important; border-color: #555 !important;
}
/* Fix 2: Primary button full width, larger target */
div[data-testid="column"] .stButton > button[kind="primary"] {
    background: #f0ede8 !important; color: #0f0f0f !important;
    border-color: #f0ede8 !important; font-weight: 700 !important;
    font-size: 0.85rem !important; padding: 10px 12px !important;
    width: 100% !important;
}

/* Botón cancelar en rojo */
.btn-cancelar button {
    border-color: #7f1d1d !important; color: #f87171 !important;
    background: #1a0a0a !important; width: 100% !important;
}
.btn-cancelar button:hover {
    background: #2a0f0f !important; border-color: #f87171 !important; color: #fca5a5 !important;
}

/* Fix 3: Navigation as pill buttons */
div[data-testid="stRadio"] > label { display: none !important; }
div[data-testid="stRadio"] > div {
    display: flex !important; gap: 6px !important; flex-wrap: wrap !important;
    background: transparent !important; border: none !important;
}
div[data-testid="stRadio"] > div > label {
    background: #1a1a1a !important; border: 1px solid #333 !important;
    border-radius: 999px !important; padding: 7px 20px !important;
    cursor: pointer !important; transition: all 0.15s !important;
    font-size: 0.82rem !important; color: #666 !important;
    font-family: 'DM Sans', sans-serif !important;
}
div[data-testid="stRadio"] > div > label:hover {
    border-color: #555 !important; color: #f0ede8 !important;
}
div[data-testid="stRadio"] > div > label > div:first-child { display: none !important; }
div[data-testid="stRadio"] > div > label:has(input:checked) {
    background: #f0ede8 !important; border-color: #f0ede8 !important;
    color: #0f0f0f !important; font-weight: 600 !important;
}

.stTabs [data-baseweb="tab-list"] {
    background: transparent; border-bottom: 1px solid #222; gap: 0;
}
.stTabs [data-baseweb="tab"] {
    font-family: 'DM Sans', sans-serif; font-size: 0.8rem; color: #555;
    background: transparent; border: none; padding: 8px 20px;
}
.stTabs [aria-selected="true"] {
    color: #f0ede8 !important; border-bottom: 2px solid #f0ede8 !important;
}
.stTabs [data-baseweb="tab-panel"] { padding-top: 1.2rem; }

.stTextInput > div > div > input,
.stNumberInput > div > div > input {
    background: #161616 !important; border-color: #333 !important;
    color: #f0ede8 !important; border-radius: 8px !important;
}
.stSelectbox > div > div {
    background: #161616 !important; border-color: #333 !important;
    color: #f0ede8 !important; border-radius: 8px !important;
}

hr { border-color: #1e1e1e !important; }
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding-top: 1rem; }
</style>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════════════════════
st.markdown(f"""
<div class="panel-header">
  <div>
    <div class="panel-title">🍽️ RestauranteBOT</div>
    <div class="panel-subtitle">Panel de operaciones · {datetime.now().strftime("%-d de %B, %Y")}</div>
  </div>
  <div style="font-size:0.8rem; color:#555;">
    <span class="live-dot"></span>En vivo
  </div>
</div>
""", unsafe_allow_html=True)

# Fix 3: Navigation as pills
seccion = st.radio(
    "Navegación",
    ["📋 Pedidos", "➕ Nuevo pedido", "🍽️ Menú", "🪑 Mesas"],
    horizontal=True,
    label_visibility="collapsed"
)
st.markdown("<br>", unsafe_allow_html=True)

# ── Despacho a cada vista ──────────────────────────────────────────────────────
if seccion == "📋 Pedidos":
    pedidos.render()
elif seccion == "➕ Nuevo pedido":
    nuevo_pedido.render()
elif seccion == "🍽️ Menú":
    menu.render()
elif seccion == "🪑 Mesas":
    mesas.render()
