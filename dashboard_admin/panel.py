"""Enrutador principal del panel: login, estilos globales y navegación lateral.

El layout raíz divide la pantalla en tres: un recuadro de navegación vertical a la
izquierda, el contenido de la vista activa en el centro, y el tablero de Pedidos
SIEMPRE abierto a la derecha (con su propio fragmento en vivo). Cada vista vive en
su propio módulo dentro de views/ para poder trabajarlas de forma independiente:
    - views/pedidos.py        → tablero en vivo (panel derecho), audio y tickets
    - views/monitor_mesas.py  → monitor maestro-detalle del salón
    - views/nuevo_pedido.py   → creación manual de pedidos
    - views/menu.py           → CRUD del menú
    - views/mesas.py          → gestión de mesas
    - views/resumen.py        → cierre de caja y ventas por día
    - views/caja.py           → arqueo de caja (apertura/cierre de turno)
"""
import streamlit as st
from dotenv import load_dotenv
from datetime import datetime

import auth
from views import (pedidos, monitor_mesas, nuevo_pedido, menu, mesas, resumen,
                   caja, cancelaciones)
from db import fecha_larga

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Restaurante · Panel",
    page_icon="🍽️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ── Login y resolución de rol (RBAC) ─────────────────────────────────────────────
# La sesión se re-deriva del query param en CADA run (ver auth.py): rol + token, así
# el auto-refresh / la recarga no desloguean ni pierden el rol. El token es por rol,
# de modo que ?r= no se puede manipular para escalar privilegios sin la contraseña.
_role = auth.resolve_role_from_params(st.query_params)
if _role:
    st.session_state["autenticado"] = True
    st.session_state["user_role"] = _role

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
          <div style='font-family:Syne,sans-serif; font-size:1.5rem; font-weight:800; color:#1a1a1a;'>Restaurante</div>
          <div style='font-size:0.82rem; color:#9ca3af; margin-top:4px;'>Panel de operaciones · Acceso restringido</div>
        </div>
        """, unsafe_allow_html=True)
        password_input = st.text_input(
            "Contraseña", type="password", placeholder="••••••••",
            label_visibility="collapsed", key="login_input"
        )
        if st.button("Entrar", key="btn_login"):
            rol = auth.role_from_credentials(password_input)
            if rol:
                auth.login(rol, password_input)
                st.rerun()
            else:
                st.error("Contraseña incorrecta")
    st.stop()

# A partir de aquí la sesión está autenticada. Si por alguna razón quedó marcada como
# autenticada sin rol (p. ej. una sesión del esquema de auth anterior tras un
# redeploy en caliente), forzamos un re-login limpio en lugar de romper.
role = st.session_state.get("user_role")
if not role:
    auth.logout()
    st.rerun()

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
    width: 100% !important; min-width: 0 !important;
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

# ── Estilos de la navegación lateral (recuadro izquierdo) ───────────────────────
# Se inyectan DESPUÉS del bloque global para ganarle por orden de cascada a la
# regla base de botón (.stButton > button). El estado activo/inactivo se distingue
# por la CLAVE del widget (st-key-nav_active / st-key-nav_inactive_*), la misma
# estrategia st-key que usan los botones semánticos del resto del panel.
st.markdown("""
<style>
.nav-brand {
    font-family: 'Syne', sans-serif; font-size: 1.15rem; font-weight: 800;
    color: #1e293b; letter-spacing: -0.3px; line-height: 1.2;
}
.nav-brand-sub {
    font-size: 0.7rem; color: #94a3b8; margin-top: 4px;
    text-transform: uppercase; letter-spacing: 1px;
}

/* Ítems de navegación: tipografía y forma comunes (activo + inactivo) */
[class*="st-key-nav_inactive"] button, .st-key-nav_active button {
    text-align: left !important; justify-content: flex-start !important;
    border-radius: 8px !important; padding: 9px 14px !important;
    font-size: 0.86rem !important; font-weight: 500 !important;
    box-shadow: none !important; min-height: 0 !important;
    transition: background 0.18s ease, color 0.18s ease, border-color 0.18s ease !important;
}
[class*="st-key-nav_inactive"] button p, .st-key-nav_active button p {
    text-align: left !important; margin: 0 !important;
}

/* Inactivo: plano, transparente, sin borde — como un ítem de menú de texto */
[class*="st-key-nav_inactive"] button {
    background: transparent !important; color: #334155 !important;
    border: 1px solid transparent !important;
}
[class*="st-key-nav_inactive"] button:hover {
    background: #f1f5f9 !important; color: #1e293b !important;
    border-color: transparent !important;
}

/* Activo: tono corporativo oscuro (slate), texto blanco, borde nítido */
.st-key-nav_active button {
    background: #1e293b !important; color: #ffffff !important;
    font-weight: 600 !important; border: 1px solid #1e293b !important;
}
.st-key-nav_active button:hover {
    background: #0f172a !important; color: #ffffff !important;
    border-color: #0f172a !important;
}

/* Pedidos: tira compacta de stats para el panel lateral angosto (reemplaza las
   5 metric-cards grandes, que partían el texto en vertical). */
.ped-stats { display: flex; flex-wrap: wrap; gap: 8px; }
.ped-stat {
    flex: 1 1 64px; background: #ffffff; border: 1px solid #e5e7eb;
    border-radius: 10px; padding: 8px 6px; text-align: center;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.ped-stat-n {
    display: block; font-family: 'Syne', sans-serif; font-weight: 800;
    font-size: clamp(0.9rem, 1.8vw, 1.3rem); line-height: 1.1;
    color: #1a1a1a; white-space: nowrap;
}
.ped-stat-l {
    display: block; font-size: 0.62rem; color: #9ca3af;
    text-transform: uppercase; letter-spacing: 0.5px; margin-top: 3px;
    white-space: nowrap;
}
</style>
""", unsafe_allow_html=True)

# Fase 1: toasts encolados por una acción del run anterior (st.toast no sobrevive
# a st.rerun(), así que se guardan en session_state y se emiten aquí, ya en el
# run siguiente, antes de pintar la vista). Ver pedidos.flash()/drain_toasts().
pedidos.drain_toasts()

# Resumen ya no es una vista propia: vive como pestaña dentro de Caja. Si una sesión
# traía 'resumen' como vista activa, la reapuntamos a Caja.
if st.session_state.get("current_view") == "resumen":
    st.session_state["current_view"] = "caja"

# Vista activa por defecto / saneada al rol: si la vista guardada no existe o el rol
# no la permite (cambio de rol, parámetro forzado), caemos a la de aterrizaje del rol.
_allowed = auth.allowed_views(role)
if st.session_state.get("current_view") not in _allowed:
    st.session_state["current_view"] = auth.DEFAULT_VIEW.get(role, _allowed[0])


# ── Etiquetas de navegación (compartidas por ambos shells) ──────────────────────
NAV_LABELS = {
    "monitor": "🖥️ Monitor",
    "menu":    "🍔 Menú",
    "mesas":   "🪑 Mesas",
    "nuevo":   "➕ Nuevo pedido",
    "caja":    "💰 Caja",
}


def _dispatch(view: str):
    """Pinta la vista activa. require_view() valida el permiso del rol (defensa en
    profundidad) ANTES de renderizar nada."""
    auth.require_view(view, role)
    if view == "caja":
        # Caja + Resumen. La pestaña Resumen (métricas de venta) SOLO se instancia si
        # el rol puede ver ingresos; caja la pierde por completo (no se crea el tab).
        if auth.can("see_revenue"):
            tab_caja, tab_resumen, tab_cancel = st.tabs(
                ["💰 Caja", "📊 Resumen", "🚫 Cancelaciones"])
            with tab_caja:
                caja.render()
            with tab_resumen:
                resumen.render()
            with tab_cancel:
                cancelaciones.render()
        else:
            caja.render()
    elif view == "monitor":
        monitor_mesas.render()
    elif view == "menu":
        menu.render()
    elif view == "mesas":
        mesas.render()
    elif view == "nuevo":
        nuevo_pedido.render()


def _nav_item(label: str, view: str):
    """Botón de navegación vertical (shell escritorio). El seleccionado usa la clave
    'nav_active'; los demás 'nav_inactive_<view>'. El CSS de arriba pinta cada estado
    por esa clave. Al pulsarlo cambia current_view y re-ejecuta."""
    activo = st.session_state["current_view"] == view
    key = "nav_active" if activo else f"nav_inactive_{view}"
    if st.button(label, key=key, use_container_width=True):
        st.session_state["current_view"] = view
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SHELL MÓVIL (mesero) · una sola columna, navegación superior, sin panel derecho
# ══════════════════════════════════════════════════════════════════════════════
def _render_mobile_shell():
    # Lienzo móvil: ancho completo, sin el panel de Pedidos de escritorio (es además
    # una superficie de cobro). Navegación arriba con objetivos táctiles grandes.
    st.markdown("""
    <style>
    .block-container { padding: 0.5rem 0.6rem 4rem 0.6rem !important; max-width: 100% !important; }
    [class*="st-key-mnav_inactive"] button, .st-key-mnav_active button {
        min-height: 52px !important; border-radius: 12px !important;
        font-size: 0.9rem !important; font-weight: 600 !important; padding: 8px 6px !important;
    }
    [class*="st-key-mnav_inactive"] button {
        background: #ffffff !important; color: #334155 !important; border: 1px solid #e5e7eb !important;
    }
    .st-key-mnav_active button {
        background: #1e293b !important; color: #ffffff !important; border: 1px solid #1e293b !important;
    }
    .st-key-btn_logout_m button { background: transparent !important; border: none !important;
        color: #9ca3af !important; font-size: 0.75rem !important; }
    </style>
    """, unsafe_allow_html=True)

    top_l, top_r = st.columns([3, 1])
    with top_l:
        st.markdown('<div class="nav-brand">Restaurante</div>'
                    '<div class="nav-brand-sub">Mesero</div>', unsafe_allow_html=True)
    with top_r:
        if st.button("Salir", key="btn_logout_m", use_container_width=True):
            auth.logout()
            st.rerun()

    vistas = auth.allowed_views(role)
    cols = st.columns(len(vistas))
    for c, v in zip(cols, vistas):
        with c:
            activo = st.session_state["current_view"] == v
            key = "mnav_active" if activo else f"mnav_inactive_{v}"
            if st.button(NAV_LABELS[v], key=key, use_container_width=True):
                st.session_state["current_view"] = v
                st.rerun()

    st.divider()
    _dispatch(st.session_state["current_view"])


# ══════════════════════════════════════════════════════════════════════════════
# SHELL ESCRITORIO (admin / caja) · navegación · contenido · pedidos en vivo
# ══════════════════════════════════════════════════════════════════════════════
def _render_desktop_shell():
    col_nav, col_content, col_pedidos = st.columns([0.7, 3.3, 2.0], gap="medium")

    with col_nav:
        with st.container(border=True):
            st.markdown(
                '<div class="nav-brand">Restaurante</div>'
                f'<div class="nav-brand-sub">{fecha_larga(datetime.now())}</div>',
                unsafe_allow_html=True,
            )
            st.divider()
            # La navegación se construye desde la matriz de acceso del rol.
            for v in auth.allowed_views(role):
                _nav_item(NAV_LABELS[v], v)
            st.divider()
            if st.button("Salir", key="btn_logout", use_container_width=True):
                auth.logout()
                st.rerun()

    with col_content:
        _dispatch(st.session_state["current_view"])

    # ── Pedidos: panel derecho SIEMPRE abierto (su propio fragmento en vivo) ──────
    with col_pedidos:
        st.markdown(
            '<div style="display:flex; align-items:center; justify-content:space-between; '
            'padding-bottom:0.5rem; margin-bottom:0.75rem; border-bottom:1px solid #e5e7eb;">'
            '<div style="font-family:Syne,sans-serif; font-size:1rem; font-weight:700; '
            'color:#1a1a1a;">📋 Pedidos</div>'
            '<div style="font-size:0.75rem; color:#9ca3af;"><span class="live-dot"></span>En vivo</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        pedidos.render()


# ── Branch raíz por rol (arregla el layout de 3 columnas en móvil) ───────────────
if role == auth.MESERO:
    _render_mobile_shell()
else:
    _render_desktop_shell()
