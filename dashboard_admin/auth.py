"""RBAC: identidad, roles y capacidades — ÚNICA fuente de verdad de permisos.

Tres roles, cada uno con su propia contraseña por variable de entorno:
    admin   → acceso total.
    caja    → todo MENOS la pestaña "Resumen de ventas" (sin métricas de venta).
    mesero  → móvil, toma de pedidos y monitoreo; SIN cobrar/caja, menú de solo lectura.

La sesión NO se confía a st.session_state (se pierde en cada rerun/refresh). En su
lugar el rol y un token se persisten en la URL (?r=<rol>&auth=<token>) y se
RE-VALIDAN en cada run recalculando el token contra la contraseña configurada del
rol. Token = sha256(f"{rol}:{password}")[:16]: distinto por rol, así que un mesero
no puede "ascender" a admin manipulando solo ?r= sin conocer la contraseña de admin.

Este módulo no importa db ni views (evita ciclos): solo streamlit, os y hashlib.
"""
import streamlit as st
import hashlib
import os

# ── Roles ────────────────────────────────────────────────────────────────────────
ADMIN, CAJA, MESERO = "admin", "caja", "mesero"
# Orden de prioridad para el login: si dos roles compartieran contraseña, gana el de
# mayor privilegio (admin > caja > mesero).
ROLES = [ADMIN, CAJA, MESERO]

# Variable de entorno que guarda la contraseña de cada rol.
_ENV = {
    ADMIN:  "PANEL_PASSWORD_ADMIN",
    CAJA:   "PANEL_PASSWORD_CAJA",
    MESERO: "PANEL_PASSWORD_MESERO",
}

# ── Matriz de acceso (vistas por rol) ────────────────────────────────────────────
# El router (panel.py) construye la navegación a partir de esto Y valida el despacho
# con require_view() (defensa en profundidad: current_view vive en session_state y
# podría quedar "pegado" en una vista prohibida tras un cambio de rol).
ROLE_VIEWS = {
    ADMIN:  ["monitor", "menu", "mesas", "nuevo", "caja"],
    CAJA:   ["monitor", "menu", "mesas", "nuevo", "caja"],
    MESERO: ["nuevo", "mesas", "monitor", "menu"],  # sin caja
}

# Vista de aterrizaje por rol (la primera tarea de cada uno).
DEFAULT_VIEW = {ADMIN: "monitor", CAJA: "monitor", MESERO: "nuevo"}

# ── Capacidades (límites de ACCIÓN, ortogonales a las vistas) ─────────────────────
# Cobrar y caja están embebidos en vistas que caja/mesero SÍ ven (Monitor, tablero
# de Pedidos), así que ocultar el ítem de navegación no basta: el candado real es por
# capacidad, comprobado en el punto de acción (botón + función de dominio).
_CAPS = {
    # cobrar: abrir el modal de pago / registrar cobros.
    "cobrar":      {ADMIN, CAJA},
    # manage_caja: abrir y cerrar turno de caja (arqueo).
    "manage_caja": {ADMIN, CAJA},
    # see_revenue: pestaña "Resumen de ventas" y cualquier total de ventas/ganancia.
    "see_revenue": {ADMIN},
    # edit_menu: crear/editar/eliminar/togglear platos y componentes.
    "edit_menu":   {ADMIN, CAJA},
}


# ── Contraseñas y tokens ──────────────────────────────────────────────────────────
def password_for(role: str) -> str:
    """Contraseña configurada de un rol (cadena vacía si no está definida).

    Compatibilidad: si PANEL_PASSWORD_ADMIN no está definida pero existe la antigua
    PANEL_PASSWORD, esta vale como contraseña de admin → un despliegue ya en marcha
    no se queda sin acceso al introducir el RBAC.
    """
    val = os.getenv(_ENV.get(role, ""), "") or ""
    if role == ADMIN and not val:
        val = os.getenv("PANEL_PASSWORD", "") or ""
    return val


def token_for(role: str, password: str) -> str:
    """Token de sesión persistible en la URL: sha256('rol:password')[:16]."""
    return hashlib.sha256(f"{role}:{password}".encode()).hexdigest()[:16]


def role_from_credentials(password: str) -> str | None:
    """Rol cuya contraseña coincide con la introducida, o None. Ignora roles sin
    contraseña configurada (para que una entrada vacía nunca autentique)."""
    if not password:
        return None
    for role in ROLES:
        cfg = password_for(role)
        if cfg and password == cfg:
            return role
    return None


def resolve_role_from_params(params) -> str | None:
    """Re-deriva y VALIDA el rol desde la URL (?r=&auth=) en cada run. Devuelve el
    rol si el token coincide con el recalculado para la contraseña vigente de ese
    rol; si no, None (sesión inválida / contraseña rotada / parámetro manipulado)."""
    role = params.get("r")
    token = params.get("auth")
    if not role or not token or role not in ROLE_VIEWS:
        return None
    cfg = password_for(role)
    if not cfg:
        return None
    return role if token == token_for(role, cfg) else None


# ── Sesión ─────────────────────────────────────────────────────────────────────
def login(role: str, password: str) -> None:
    """Marca la sesión autenticada y persiste rol+token en la URL."""
    st.session_state["autenticado"] = True
    st.session_state["user_role"] = role
    st.query_params["r"] = role
    st.query_params["auth"] = token_for(role, password)


def logout() -> None:
    """Cierra la sesión: limpia la URL y el estado de autenticación."""
    for k in ("r", "auth"):
        try:
            del st.query_params[k]
        except KeyError:
            pass
    st.session_state["autenticado"] = False
    st.session_state.pop("user_role", None)


def current_role() -> str | None:
    return st.session_state.get("user_role")


# ── Comprobaciones (las usan el router y las vistas) ─────────────────────────────
def can(cap: str) -> bool:
    """True si el rol actual tiene la capacidad pedida."""
    return current_role() in _CAPS.get(cap, set())


def allowed_views(role: str | None = None) -> list:
    return ROLE_VIEWS.get(role or current_role(), [])


def view_allowed(view: str, role: str | None = None) -> bool:
    return view in allowed_views(role)


def require_view(view: str, role: str | None = None) -> None:
    """Guard de despacho: si el rol no puede ver esta vista, error y alto total.
    Cubre el caso de una vista prohibida que quedó 'pegada' en session_state o un
    acceso forzado por query param de un rol inferior."""
    if not view_allowed(view, role):
        st.error("🔒 Acceso denegado")
        st.stop()
