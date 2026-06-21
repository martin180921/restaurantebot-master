"""Perfiles persistentes de personal + marcaje de turno (FASE 1).

Distinto de mesero_keys.py (PIN EFÍMERO de turno, legado que se revoca al cerrar caja):
aquí un empleado es un perfil PERSISTENTE (mesero/caja/admin) con un PIN propio (solo se
guarda el hash) que no caduca. Es la fuente de identidad del actor para la auditoría.

También gestiona el marcaje entrada/salida (clock-in/out) en sesiones_empleado, y valida
el PIN de administrador que desbloquea los descuentos (admin_pin_valido).

Importa db (engine) y auth (solo para leer la contraseña de admin de entorno, sin ciclo).
La auditoría de altas/bajas la dispara la vista; aquí solo la lógica de datos.
"""
import hashlib
import secrets

from sqlalchemy import text

import auth
from db import engine

ROLES_VALIDOS = ("mesero", "caja", "admin")


def _hash(pin: str) -> str:
    return hashlib.sha256(str(pin or "").strip().encode()).hexdigest()


def _pin_aleatorio() -> str:
    """PIN numérico de 6 dígitos (fácil de teclear en móvil)."""
    return f"{secrets.randbelow(1_000_000):06d}"


# ── CRUD de perfiles ──────────────────────────────────────────────────────────────
def crear_empleado(nombre: str, rol: str, pin: str = "", creado_por: str = "") -> tuple:
    """Crea un empleado activo. Si 'pin' viene vacío, genera uno de 6 dígitos.

    Devuelve (pin_en_claro, error). El PIN se muestra UNA vez. error es None si OK, o un
    mensaje legible (nombre vacío, rol inválido, PIN en uso, PIN no numérico, fallo de BD).
    """
    nombre = (nombre or "").strip()[:120]
    rol = (rol or "").strip().lower()
    if not nombre:
        return None, "El nombre es obligatorio."
    if rol not in ROLES_VALIDOS:
        return None, "Rol inválido."
    pin = (pin or "").strip()
    if pin and (not pin.isdigit() or len(pin) < 4):
        return None, "El PIN debe ser numérico de al menos 4 dígitos."
    try:
        with engine.begin() as conn:
            for intento in range(6):
                actual = pin or _pin_aleatorio()
                h = _hash(actual)
                choca = conn.execute(text(
                    "SELECT 1 FROM empleados WHERE pin_hash = :h AND activo = TRUE"
                ), {"h": h}).first()
                if choca:
                    if pin:                       # PIN elegido por el usuario y ya en uso
                        return None, "Ese PIN ya está en uso. Elige otro."
                    continue                      # PIN aleatorio: reintenta con otro
                conn.execute(text(
                    "INSERT INTO empleados (nombre, rol, pin_hash, creado_por) "
                    "VALUES (:n, :r, :h, :cp)"
                ), {"n": nombre, "r": rol, "h": h,
                    "cp": (creado_por or "").strip()[:120] or None})
                return actual, None
    except Exception:
        return None, "No se pudo crear el empleado (error de base de datos)."
    return None, "No se pudo generar un PIN único. Intenta de nuevo."


def listar_empleados(incluir_inactivos: bool = True) -> list:
    """[{id, nombre, rol, activo, creado}] de empleados. Tolerante a fallos."""
    cond = "" if incluir_inactivos else "WHERE activo = TRUE"
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT id, nombre, rol, activo, creado FROM empleados {cond} "
                "ORDER BY activo DESC, nombre"
            )).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


def desactivar_empleado(emp_id: int) -> bool:
    """Baja (soft-delete) de un empleado: activo=FALSE. Su PIN deja de servir. Devuelve
    True si cambió algo. También cierra su sesión de turno abierta, si la hubiera."""
    try:
        with engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE empleados SET activo = FALSE WHERE id = :id AND activo = TRUE"
            ), {"id": int(emp_id)})
            conn.execute(text(
                "UPDATE sesiones_empleado SET activa = FALSE, logout_at = NOW() "
                "WHERE empleado_id = :id AND activa = TRUE"
            ), {"id": int(emp_id)})
        return (res.rowcount or 0) > 0
    except Exception:
        return False


def regenerar_pin(emp_id: int) -> tuple:
    """Asigna un PIN nuevo (6 dígitos) a un empleado activo. Devuelve (pin, error)."""
    try:
        with engine.begin() as conn:
            existe = conn.execute(text(
                "SELECT 1 FROM empleados WHERE id = :id AND activo = TRUE"
            ), {"id": int(emp_id)}).first()
            if not existe:
                return None, "Empleado no encontrado o inactivo."
            for _ in range(6):
                pin = _pin_aleatorio()
                h = _hash(pin)
                choca = conn.execute(text(
                    "SELECT 1 FROM empleados WHERE pin_hash = :h AND activo = TRUE AND id <> :id"
                ), {"h": h, "id": int(emp_id)}).first()
                if choca:
                    continue
                conn.execute(text("UPDATE empleados SET pin_hash = :h WHERE id = :id"),
                             {"h": h, "id": int(emp_id)})
                return pin, None
    except Exception:
        return None, "No se pudo regenerar el PIN (error de base de datos)."
    return None, "No se pudo generar un PIN único. Intenta de nuevo."


# ── Autenticación por PIN ─────────────────────────────────────────────────────────
def validar_pin(pin: str):
    """{id, nombre, rol} del empleado ACTIVO cuyo PIN coincide, o None. Para el login."""
    pin = (pin or "").strip()
    if not pin:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT id, nombre, rol FROM empleados WHERE pin_hash = :h AND activo = TRUE "
                "ORDER BY id DESC LIMIT 1"
            ), {"h": _hash(pin)}).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None


def admin_pin_valido(pin: str):
    """Nombre del autorizador si 'pin' corresponde a un ADMIN válido, o None.

    Acepta dos fuentes: (1) el PIN de un empleado activo con rol 'admin', o (2) la
    contraseña de admin de entorno (PANEL_PASSWORD_ADMIN) como llave maestra. Esto
    desbloquea los descuentos/cortesías: la rebaja se registra a nombre de quien autoriza.
    """
    pin = (pin or "").strip()
    if not pin:
        return None
    # (2) Llave maestra de entorno → autorizador genérico 'Admin (maestro)'.
    maestro = auth.password_for(auth.ADMIN)
    if maestro and pin == maestro:
        return "Admin (maestro)"
    # (1) Empleado admin activo.
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT nombre FROM empleados WHERE pin_hash = :h AND activo = TRUE "
                "AND rol = 'admin' ORDER BY id DESC LIMIT 1"
            ), {"h": _hash(pin)}).mappings().first()
        return row["nombre"] if row else None
    except Exception:
        return None


# ── Marcaje de turno (clock-in / clock-out) ──────────────────────────────────────
def abrir_sesion(nombre: str, rol: str, empleado_id=None) -> int | None:
    """Marca entrada (clock-in) y devuelve el id de sesión. Cierra primero cualquier
    sesión abierta del mismo empleado (evita duplicados si reentra desde otro dispositivo).
    Tolerante a fallos → None (el login NO debe romperse porque el marcaje falle)."""
    try:
        with engine.begin() as conn:
            if empleado_id is not None:
                conn.execute(text(
                    "UPDATE sesiones_empleado SET activa = FALSE, logout_at = NOW() "
                    "WHERE empleado_id = :id AND activa = TRUE"
                ), {"id": int(empleado_id)})
            row = conn.execute(text(
                "INSERT INTO sesiones_empleado (empleado_id, nombre, rol) "
                "VALUES (:eid, :n, :r) RETURNING id"
            ), {"eid": (int(empleado_id) if empleado_id is not None else None),
                "n": (nombre or "")[:120], "r": (rol or "")[:20]}).scalar_one()
        return int(row)
    except Exception:
        return None


def cerrar_sesion(sesion_id) -> dict | None:
    """Marca salida (clock-out) de una sesión y devuelve {nombre, rol} para auditar.
    Tolerante a fallos → None."""
    if not sesion_id:
        return None
    try:
        with engine.begin() as conn:
            row = conn.execute(text(
                "UPDATE sesiones_empleado SET activa = FALSE, logout_at = NOW() "
                "WHERE id = :id AND activa = TRUE RETURNING nombre, rol"
            ), {"id": int(sesion_id)}).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None


def cerrar_todas_sesiones() -> int:
    """Cierra TODAS las sesiones activas (fin de jornada / cierre de caja). Devuelve
    cuántas se cerraron."""
    try:
        with engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE sesiones_empleado SET activa = FALSE, logout_at = NOW() WHERE activa = TRUE"
            ))
        return res.rowcount or 0
    except Exception:
        return 0


def sesiones_activas() -> list:
    """[{id, empleado_id, nombre, rol, login_at}] de quién está en turno AHORA.
    Tolerante a fallos."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, empleado_id, nombre, rol, login_at FROM sesiones_empleado "
                "WHERE activa = TRUE ORDER BY login_at"
            )).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []
