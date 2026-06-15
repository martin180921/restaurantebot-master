"""Enrutador principal del panel: login, estilos globales y navegación.

Cada pestaña vive en su propio módulo dentro de views/ para poder trabajarlas
de forma independiente:
    - views/pedidos.py        → tablero, alertas de audio y tickets
    - views/monitor_mesas.py  → monitor maestro-detalle del salón
    - views/nuevo_pedido.py   → creación manual de pedidos
    - views/menu.py           → CRUD del menú
    - views/mesas.py          → gestión de mesas
"""
import streamlit as st
from dotenv import load_dotenv
import hashlib
import os
from datetime import datetime

from views import pedidos, monitor_mesas, nuevo_pedido, menu, mesas, resumen, caja
from db import fecha_larga

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
    .stApp { background: #f7f7f5; color: #1a1a1a; }
    header[data-testid="stHeader"], [data-testid="stToolbar"],
    [data-testid="stDecoration"], [data-testid="stStatusWidget"],
    [data-testid="stMainMenu"], #MainMenu, footer { display: none !important; }
    .stTextInput > div > div > input {
        background: #ffffff !important; border-color: #d1d5db !important;
        color: #1a1a1a !important; border-radius: 8px !important;
        text-align: center; font-size: 1.1rem; letter-spacing: 4px;
    }
    .stButton > button {
        width: 100%; background: #1a1a1a !important; color: #ffffff !important;
        border: none !important; border-radius: 8px !important;
        font-family: 'DM Sans', sans-serif !important;
        font-weight: 600 !important; font-size: 0.9rem !important;
        padding: 10px !important; margin-top: 8px !important;
    }
    .stButton > button:hover { background: #374151 !important; }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("<div style='height: 8vh'></div>", unsafe_allow_html=True)
    col_c, col_i, col_c2 = st.columns([1, 2, 1])
    with col_i:
        st.markdown("""
        <div style='text-align:center; margin-bottom: 2rem;'>
          <div style='font-family:Syne,sans-serif; font-size:1.5rem; font-weight:800; color:#1a1a1a;'>🍽️ RestauranteBOT</div>
          <div style='font-size:0.82rem; color:#9ca3af; margin-top:4px;'>Panel de operaciones · Acceso restringido</div>
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

# ── Auto-refresh (30s) — C1 + P2 + P4 ──────────────────────────────────────────
# El refresco en vivo vive ahora dentro de cada vista como un st.fragment
# (run_every="30s"): solo se re-ejecuta ESE fragmento, no panel.py ni la app
# entera, así que PRESERVA st.session_state y la alerta de audio, y no relanza la
# app mientras se arma un pedido en otras pestañas. (Antes: st_autorefresh.)

# ── Estilos (Light Mode) ───────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:wght@300;400;500&display=swap');

html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
.stApp { background: #f7f7f5; color: #1a1a1a; }

.panel-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 1.25rem 0 1rem 0; border-bottom: 1px solid #e5e7eb; margin-bottom: 1.25rem;
}
.panel-title {
    font-family: 'Syne', sans-serif; font-size: 1.6rem;
    font-weight: 800; color: #1a1a1a; letter-spacing: -0.5px;
}
.panel-subtitle { font-size: 0.8rem; color: #6b7280; margin-top: 2px; }
.live-dot {
    width: 8px; height: 8px; background: #22c55e; border-radius: 50%;
    display: inline-block; margin-right: 6px; animation: pulse 1.8s infinite;
}
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

.metric-card {
    background: #ffffff; border: 1px solid #e5e7eb;
    border-radius: 12px; padding: 1.2rem 1.5rem; text-align: center;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.metric-value {
    font-family: 'Syne', sans-serif; font-size: 2.4rem;
    font-weight: 800; color: #1a1a1a; line-height: 1;
}
.metric-label {
    font-size: 0.72rem; color: #9ca3af;
    text-transform: uppercase; letter-spacing: 1px; margin-top: 4px;
}
.metric-accent { color: #d97706; }
.metric-green  { color: #16a34a; }
.metric-blue   { color: #2563eb; }

.badge {
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 0.72rem; font-weight: 500; letter-spacing: 0.3px;
}
.badge-pendiente   { background: #fef3c7; color: #b45309; border: 1px solid #fde68a; }
.badge-preparacion { background: #dbeafe; color: #1d4ed8; border: 1px solid #bfdbfe; }
.badge-listo       { background: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }
.badge-entregado   { background: #f3f4f6; color: #6b7280; border: 1px solid #e5e7eb; }
.badge-cancelado   { background: #fee2e2; color: #b91c1c; border: 1px solid #fecaca; }
.badge-activo      { background: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }
.badge-inactivo    { background: #f3f4f6; color: #9ca3af; border: 1px solid #e5e7eb; }
.badge-agotado     { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }

.order-card {
    background: #ffffff; border: 1px solid #e5e7eb; border-radius: 14px;
    padding: 1.2rem 1.4rem; margin-bottom: 0.8rem; transition: border-color 0.2s;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.order-card:hover { border-color: #d1d5db; }
.order-id    { font-family: 'Syne', sans-serif; font-size: 0.75rem; color: #9ca3af; }
.order-num   { font-size: 0.9rem; font-weight: 500; color: #1a1a1a; }
.order-items { font-size: 0.82rem; color: #6b7280; margin: 4px 0; }
.order-total { font-family: 'Syne', sans-serif; font-size: 1.1rem; font-weight: 700; color: #1a1a1a; }
.order-fecha { font-size: 0.72rem; color: #9ca3af; }

.menu-card {
    background: #ffffff; border: 1px solid #e5e7eb; border-radius: 14px;
    padding: 1rem 1.2rem; margin-bottom: 0.6rem; transition: border-color 0.2s;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.menu-card:hover { border-color: #d1d5db; }
.menu-card.inactivo { opacity: 0.55; }
.menu-nombre { font-family: 'Syne', sans-serif; font-size: 1rem; font-weight: 700; color: #1a1a1a; }
.menu-precio { font-size: 0.85rem; color: #6b7280; margin-top: 2px; }

.section-title {
    font-family: 'Syne', sans-serif; font-size: 1rem;
    font-weight: 700; color: #1a1a1a; margin-bottom: 1rem;
    padding-bottom: 0.5rem; border-bottom: 1px solid #e5e7eb;
}

/* All buttons base */
.stButton > button {
    border-radius: 8px !important; font-family: 'DM Sans', sans-serif !important;
    font-size: 0.78rem !important; font-weight: 500 !important;
    border: 1px solid #d1d5db !important; background: #ffffff !important;
    color: #374151 !important; padding: 6px 12px !important;
    transition: all 0.15s !important; height: auto !important;
    width: 100% !important;
}
.stButton > button:hover {
    background: #f3f4f6 !important; color: #111827 !important; border-color: #9ca3af !important;
}
/* Primary button full width, larger target */
div[data-testid="stColumn"] .stButton > button[kind="primary"] {
    background: #1a1a1a !important; color: #ffffff !important;
    border-color: #1a1a1a !important; font-weight: 700 !important;
    font-size: 0.85rem !important; padding: 10px 12px !important;
    width: 100% !important;
}

/* ── Fase 2: colores semánticos de botones de acción (estrategia st-key) ───────
   El personal opera por COLOR, no leyendo texto. Se afina por clave de widget
   (st-key-<key>, Streamlit >= 1.39). Las variantes ancladas en
   `div[data-testid="stColumn"] … .stButton > button` igualan la especificidad de
   la regla de botón primario (negra) de arriba y, al declararse DESPUÉS, le ganan
   por orden — así el color semántico prevalece sobre el negro en los primary. La
   clase st-key-<key> vive en el stElementContainer (ancestro del .stButton), por
   eso el descendiente `.stButton > button` resuelve bien.
   Nota: Streamlit usa data-testid="stColumn" (no "column") desde 1.39+. */

/* Verde vibrante → avanzar estado / cobrar / guardar / confirmar (positivas). */
[class*="st-key-avanzar_"] button, [class*="st-key-cobrar_"] button,
[class*="st-key-mon_cobrar_mesa_"] button, [class*="st-key-confirm_cobrar_"] button,
[class*="st-key-btn_guardar"] button, [class*="st-key-btn_confirmar"] button,
div[data-testid="stColumn"] [class*="st-key-avanzar_"] .stButton > button,
div[data-testid="stColumn"] [class*="st-key-mon_cobrar_mesa_"] .stButton > button,
div[data-testid="stColumn"] [class*="st-key-confirm_cobrar_"] .stButton > button,
div[data-testid="stColumn"] [class*="st-key-btn_guardar"] .stButton > button,
div[data-testid="stColumn"] [class*="st-key-btn_confirmar"] .stButton > button {
    background: #16a34a !important; border-color: #16a34a !important;
    color: #ffffff !important; font-weight: 700 !important;
}
[class*="st-key-avanzar_"] button:hover, [class*="st-key-cobrar_"] button:hover,
[class*="st-key-mon_cobrar_mesa_"] button:hover, [class*="st-key-confirm_cobrar_"] button:hover,
[class*="st-key-btn_guardar"] button:hover, [class*="st-key-btn_confirmar"] button:hover,
div[data-testid="stColumn"] [class*="st-key-avanzar_"] .stButton > button:hover,
div[data-testid="stColumn"] [class*="st-key-mon_cobrar_mesa_"] .stButton > button:hover,
div[data-testid="stColumn"] [class*="st-key-confirm_cobrar_"] .stButton > button:hover,
div[data-testid="stColumn"] [class*="st-key-btn_guardar"] .stButton > button:hover,
div[data-testid="stColumn"] [class*="st-key-btn_confirmar"] .stButton > button:hover {
    background: #15803d !important; border-color: #15803d !important; color: #ffffff !important;
}

/* Rojo oscuro → cancelar / eliminar (acciones destructivas). */
[class*="st-key-cancelar_"] button, [class*="st-key-confirm_cancel_"] button,
[class*="st-key-eliminar_"] button, [class*="st-key-confirm_eliminar_"] button,
div[data-testid="stColumn"] [class*="st-key-confirm_cancel_"] .stButton > button,
div[data-testid="stColumn"] [class*="st-key-confirm_eliminar_"] .stButton > button {
    background: #b91c1c !important; border-color: #b91c1c !important;
    color: #ffffff !important; font-weight: 700 !important;
}
[class*="st-key-cancelar_"] button:hover, [class*="st-key-confirm_cancel_"] button:hover,
[class*="st-key-eliminar_"] button:hover, [class*="st-key-confirm_eliminar_"] button:hover,
div[data-testid="stColumn"] [class*="st-key-confirm_cancel_"] .stButton > button:hover,
div[data-testid="stColumn"] [class*="st-key-confirm_eliminar_"] .stButton > button:hover {
    background: #991b1b !important; border-color: #991b1b !important; color: #ffffff !important;
}

/* Gris neutro → imprimir ticket. */
[class*="st-key-ticket_"] button,
div[data-testid="stColumn"] [class*="st-key-ticket_"] .stButton > button {
    background: #6b7280 !important; border-color: #6b7280 !important; color: #ffffff !important;
}
[class*="st-key-ticket_"] button:hover,
div[data-testid="stColumn"] [class*="st-key-ticket_"] .stButton > button:hover {
    background: #4b5563 !important; border-color: #4b5563 !important; color: #ffffff !important;
}

/* Navigation as pill buttons */
div[data-testid="stRadio"] > label { display: none !important; }
div[data-testid="stRadio"] > div {
    display: flex !important; gap: 6px !important; flex-wrap: wrap !important;
    background: transparent !important; border: none !important;
}
div[data-testid="stRadio"] > div > label {
    background: #ffffff !important; border: 1px solid #e5e7eb !important;
    border-radius: 999px !important; padding: 7px 20px !important;
    cursor: pointer !important; transition: all 0.15s !important;
    font-size: 0.82rem !important; color: #6b7280 !important;
    font-family: 'DM Sans', sans-serif !important;
}
div[data-testid="stRadio"] > div > label:hover {
    border-color: #9ca3af !important; color: #1a1a1a !important;
}
div[data-testid="stRadio"] > div > label > div:first-child { display: none !important; }
div[data-testid="stRadio"] > div > label:has(input:checked) {
    background: #1a1a1a !important; border-color: #1a1a1a !important;
    color: #ffffff !important; font-weight: 600 !important;
}

.stTabs [data-baseweb="tab-list"] {
    background: transparent; border-bottom: 1px solid #e5e7eb; gap: 0;
}
.stTabs [data-baseweb="tab"] {
    font-family: 'DM Sans', sans-serif; font-size: 0.8rem; color: #9ca3af;
    background: transparent; border: none; padding: 8px 20px;
}
.stTabs [aria-selected="true"] {
    color: #1a1a1a !important; border-bottom: 2px solid #1a1a1a !important;
}
.stTabs [data-baseweb="tab-panel"] { padding-top: 1.2rem; }

.stTextInput > div > div > input,
.stNumberInput > div > div > input {
    background: #ffffff !important; border-color: #d1d5db !important;
    color: #1a1a1a !important; border-radius: 8px !important;
}
.stSelectbox > div > div {
    background: #ffffff !important; border-color: #d1d5db !important;
    color: #1a1a1a !important; border-radius: 8px !important;
}

hr { border-color: #e5e7eb !important; }

/* Fase 1: lienzo limpio — ocultar por completo la cromática de Streamlit
   (cabecera superior, menú hamburguesa, barra de estado, decoración y pie).
   display:none (no visibility:hidden) para que NO reserven espacio vertical. */
header[data-testid="stHeader"], [data-testid="stToolbar"],
[data-testid="stDecoration"], [data-testid="stStatusWidget"],
[data-testid="stMainMenu"], #MainMenu, footer { display: none !important; }

/* Req 1: tighter spacing to reclaim vertical space on mobile/tablet */
.block-container { padding: 0.75rem 1rem 1rem 1rem !important; }
.element-container { margin-bottom: 0.35rem !important; }
.stButton > button { margin: 2px 0 !important; }

/* F1/UX: botones de acción apilados (columna de acciones del tablero) más juntos.
   El gran hueco venía del margen por contenedor de cada botón. */
div[data-testid="stColumn"] .element-container:has(.stButton) { margin-bottom: 0 !important; }
div[data-testid="stColumn"] .stButton > button { margin: 1px 0 !important; }
</style>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════════════════════
st.markdown(f"""
<div class="panel-header">
  <div>
    <div class="panel-title">🍽️ RestauranteBOT</div>
    <div class="panel-subtitle">Panel de operaciones · {fecha_larga(datetime.now())}</div>
  </div>
  <div style="font-size:0.8rem; color:#9ca3af;">
    <span class="live-dot"></span>En vivo
  </div>
</div>
""", unsafe_allow_html=True)

# Fix 3: Navigation as pills
seccion = st.radio(
    "Navegación",
    ["📋 Pedidos", "🖥️ Monitor", "➕ Nuevo pedido", "🍽️ Menú", "🪑 Mesas", "📊 Resumen", "💰 Caja"],
    horizontal=True,
    label_visibility="collapsed"
)
st.markdown("<br>", unsafe_allow_html=True)

# Fase 1: toasts encolados por una acción del run anterior (st.toast no sobrevive
# a st.rerun(), así que se guardan en session_state y se emiten aquí, ya en el
# run siguiente, antes de pintar la vista). Ver pedidos.flash()/drain_toasts().
pedidos.drain_toasts()

# ── Despacho a cada vista ──────────────────────────────────────────────────────
if seccion == "📋 Pedidos":
    pedidos.render()
elif seccion == "🖥️ Monitor":
    monitor_mesas.render()
elif seccion == "➕ Nuevo pedido":
    nuevo_pedido.render()
elif seccion == "🍽️ Menú":
    menu.render()
elif seccion == "🪑 Mesas":
    mesas.render()
elif seccion == "📊 Resumen":
    resumen.render()
elif seccion == "💰 Caja":
    caja.render()
