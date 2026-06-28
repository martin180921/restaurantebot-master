"""RBAC: identidad, roles y capacidades — ÚNICA fuente de verdad de permisos.

Tres roles:
    admin   → acceso total. Contraseña por variable de entorno.
    caja    → todo MENOS la pestaña "Resumen de ventas". Contraseña por variable de entorno.
    mesero  → móvil, toma de pedidos y monitoreo; SIN cobrar/caja, menú de solo lectura.
              Acceso SOLO con un PIN de turno efímero generado en caja (ver mesero_keys.py);
              ya NO usa contraseña fija de entorno.

Sesión POR CONEXIÓN: la autenticación vive solo en st.session_state (memoria de ESTA
pestaña/dispositivo). Antes se persistía en la URL (?r=&auth=token), pero eso convertía
el enlace en una credencial: compartir el link o abrir otra pestaña heredaba la sesión
sin contraseña. Ahora cada conexión (dispositivo, pestaña o link compartido) debe
autenticarse de forma independiente; un refresco completo (F5) también pide login de nuevo.
El auto-refresco normal usa st.fragment(run_every=…) y st.rerun(), que CONSERVAN
session_state, así que no desloguea en operación normal — solo una recarga del navegador.

Este módulo no importa db ni views (evita ciclos): solo streamlit, os y hashlib.
"""
import streamlit as st
import os

# ── Roles ────────────────────────────────────────────────────────────────────────
ADMIN, CAJA, MESERO = "admin", "caja", "mesero"
ROLES = [ADMIN, CAJA, MESERO]
# Roles que se autentican con contraseña fija de entorno (mesero es solo-PIN).
ROLES_PASSWORD = [ADMIN, CAJA]

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
    # 'admin' (Administración) va al FINAL → su ítem de navegación queda al fondo del
    # menú lateral. Entorno SOLO-ADMIN, aislado del flujo operativo: agrupa Resumen de
    # ventas, Cancelaciones y Personal (marcaje de turno) en pestañas.
    ADMIN:  ["monitor", "menu", "mesas", "nuevo", "caja", "meseros", "admin"],
    CAJA:   ["monitor", "menu", "mesas", "nuevo", "caja", "meseros"],
    # Mesero NO gestiona mesas (crear/editar/borrar es tarea de caja/admin): solo toma
    # pedidos (nuevo), ve el salón en vivo (monitor) y consulta el menú (solo lectura).
    MESERO: ["nuevo", "monitor", "menu"],  # sin caja, sin gestión de mesas ni meseros
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
    # manage_empleados: crear/dar de baja/regenerar PIN de perfiles de personal. SOLO
    # admin (caja conserva la generación de PINs de turno EFÍMEROS, no perfiles fijos).
    "manage_empleados": {ADMIN},
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


def role_from_credentials(password: str) -> str | None:
    """Rol (admin/caja) cuya contraseña de entorno coincide con la introducida, o None.
    El mesero NO entra por aquí: se autentica con un PIN de turno (ver mesero_keys.py)."""
    if not password:
        return None
    for role in ROLES_PASSWORD:
        cfg = password_for(role)
        if cfg and password == cfg:
            return role
    return None


# ── Sesión (por conexión: solo st.session_state) ────────────────────────────────
# Identidad del ACTOR (FASE 1): nombre + rol que la auditoría estampa en cada evento.
# Vive en session_state (puro, sin BD). El marcaje de turno (sesiones_empleado) y la
# escritura del log los orquesta panel.py/empleados.py, que SÍ tocan BD; auth solo
# guarda la identidad para que audit.registrar() sepa a quién atribuir la acción.
_ROL_LABEL = {ADMIN: "Admin (maestro)", CAJA: "Caja (maestro)", MESERO: "Mesero"}


def _set_actor(nombre: str, rol: str) -> None:
    st.session_state["actor_nombre"] = nombre
    st.session_state["actor_rol"] = rol


def login(role: str, nombre: str = None) -> None:
    """Marca la sesión autenticada SOLO en session_state (esta pestaña/dispositivo).
    Ya no se escribe nada en la URL → el enlace no es una credencial compartible.
    'nombre' identifica al actor en la auditoría (default: etiqueta genérica del rol)."""
    st.session_state["autenticado"] = True
    st.session_state["user_role"] = role
    st.session_state.pop("mesero_key_id", None)   # limpia un PIN previo si cambia de rol
    st.session_state.pop("empleado_id", None)
    _set_actor(nombre or _ROL_LABEL.get(role, role), role)


def login_empleado(emp: dict) -> None:
    """Autentica con un perfil de empleado persistente (empleados.py): rol e identidad
    salen del perfil, así la auditoría atribuye cada acción a la persona concreta."""
    st.session_state["autenticado"] = True
    st.session_state["user_role"] = emp["rol"]
    st.session_state["empleado_id"] = int(emp["id"])
    st.session_state.pop("mesero_key_id", None)
    _set_actor(emp["nombre"], emp["rol"])


def login_mesero(key_id: int, nombre: str = None) -> None:
    """Autentica como mesero contra un PIN de turno EFÍMERO (legado). Guarda el id de la
    clave para revalidarla en cada run (revocación inmediata desde caja)."""
    st.session_state["autenticado"] = True
    st.session_state["user_role"] = MESERO
    st.session_state["mesero_key_id"] = int(key_id)
    st.session_state.pop("empleado_id", None)
    _set_actor(nombre or _ROL_LABEL[MESERO], MESERO)


def logout() -> None:
    """Cierra la sesión: limpia session_state y la URL (restos del esquema antiguo ?r=&auth=
    y el token de persistencia del mesero ?mt, para que no quede acceso pegado al enlace)."""
    for k in ("r", "auth", "mt"):
        try:
            del st.query_params[k]
        except KeyError:
            pass
    st.session_state["autenticado"] = False
    for k in ("user_role", "mesero_key_id", "empleado_id",
              "actor_nombre", "actor_rol", "sesion_id"):
        st.session_state.pop(k, None)


def current_role() -> str | None:
    return st.session_state.get("user_role")


def actor() -> tuple:
    """(nombre, rol) del usuario en sesión, para la auditoría. Defaults seguros."""
    rol = current_role() or ""
    return (st.session_state.get("actor_nombre") or _ROL_LABEL.get(rol, "Desconocido"), rol)


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
