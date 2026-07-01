"""'Recuérdame' de admin/caja vía COOKIE del navegador (no URL).

auth.py documenta por qué admin/caja dejaron de persistir la sesión: antes se guardaba
un token en la URL (?r=&auth=) y eso convertía el enlace en una credencial compartible
(compartir el link o hacer una captura de pantalla filtraba el acceso). Este módulo
resuelve la molestia de tener que reloguear en cada F5 o reapertura del navegador SIN
repetir ese error: el token vive en una cookie (invisible, no viaja al copiar el enlace,
no queda en logs de acceso) y caduca sola a las REMEMBER_HORAS de emitida.

Solo se guarda el HASH del token (mismo patrón que empleados.pin_hash); el valor en claro
sale una única vez de crear() para dejarlo en la cookie. panel.py orquesta cuándo se emite
(login de admin/caja), se valida (reconexión sin sesión en memoria) y se revoca (logout
explícito). Importa db (a diferencia de auth.py, que se mantiene sin BD para evitar ciclos:
auth no importa este módulo).
"""
import hashlib
import os
import secrets

from sqlalchemy import text

from db import engine

COOKIE_NAME = "rbt_rsess"


def _horas_default() -> int:
    try:
        return max(1, int(os.getenv("REMEMBER_HORAS", "12")))
    except (TypeError, ValueError):
        return 12


HORAS_DEFAULT = _horas_default()


def _hash(token: str) -> str:
    return hashlib.sha256(str(token or "").strip().encode()).hexdigest()


def crear(rol: str, empleado_id: int | None, nombre: str, horas: int = None) -> str:
    """Genera un token nuevo, guarda su HASH con expiración y devuelve el token EN CLARO
    (solo aquí: es lo único que se deja en la cookie). Cadena vacía si falla la BD —
    tolerante, no debe romper el login."""
    token = secrets.token_hex(32)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO sesiones_recordadas (token_hash, rol, empleado_id, nombre, expira) "
                "VALUES (:h, :r, :eid, :n, NOW() + make_interval(hours => :hrs))"
            ), {"h": _hash(token), "r": rol,
                "eid": (int(empleado_id) if empleado_id is not None else None),
                "n": (nombre or "")[:120], "hrs": int(horas or HORAS_DEFAULT)})
        return token
    except Exception:
        return ""


def validar(token: str):
    """{rol, empleado_id, nombre} si el token está vigente (no expiró) y, cuando está
    ligado a un perfil de empleado, este sigue activo y no bloqueado. None si no vale.
    Tolerante a fallos de BD (devuelve None: ante la duda, se vuelve a pedir login)."""
    token = (token or "").strip()
    if not token:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT sr.rol, sr.empleado_id, sr.nombre FROM sesiones_recordadas sr "
                "LEFT JOIN empleados e ON e.id = sr.empleado_id "
                "WHERE sr.token_hash = :h AND sr.expira > NOW() "
                "AND (sr.empleado_id IS NULL OR (e.activo = TRUE AND e.bloqueado = FALSE))"
            ), {"h": _hash(token)}).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None


def revocar(token: str) -> None:
    """Borra el token (logout explícito): la cookie vieja deja de servir de inmediato."""
    token = (token or "").strip()
    if not token:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM sesiones_recordadas WHERE token_hash = :h"),
                         {"h": _hash(token)})
    except Exception:
        pass


def limpiar_expiradas() -> int:
    """Housekeeping: borra tokens ya vencidos. Devuelve cuántos borró (0 ante fallo)."""
    try:
        with engine.begin() as conn:
            res = conn.execute(text("DELETE FROM sesiones_recordadas WHERE expira <= NOW()"))
        return res.rowcount or 0
    except Exception:
        return 0
