"""Freno de fuerza bruta para el login del panel (FASE 1, endurecimiento).

El login del panel acepta un PIN de 6 dígitos (empleado o turno) o una contraseña de rol.
Un PIN numérico tiene solo 1 millón de combinaciones: sin freno, cualquiera con la URL
pública podía automatizar el barrido. Aquí llevamos un registro de intentos FALLIDOS por
cliente (la IP del X-Forwarded-For tras el proxy de Railway, o 'global' si no se puede
determinar) en una ventana deslizante y bloqueamos temporalmente al superar el umbral.

Se guarda SOLO el hecho del fallo (ip + hora), nunca el PIN/contraseña probado. La clave es
por IP (no global) para que un atacante no pueda dejar sin acceso a todo el personal (un
bloqueo global sería un DoS trivial); la caja opera desde su propia IP y no se ve afectada
por intentos de otra.

Tolerante a fallos de BD: ante la duda NO bloquea (un blip de red no debe dejar sin acceso
al personal). El freno es una capa extra sobre el login por hash, no la única defensa.

Vive aparte de auth.py (que se mantiene sin BD) y comparte el engine de db.py. El panel
calcula la IP (usa st.context) y la pasa aquí; este módulo solo toca la BD.
"""
import os

from sqlalchemy import text

from db import engine


def _int_env(nombre: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(nombre, str(default))))
    except (TypeError, ValueError):
        return default


# Umbral: MAX_FALLOS intentos fallidos dentro de VENTANA_MIN minutos disparan el bloqueo,
# que dura BLOQUEO_MIN minutos contados desde el ÚLTIMO fallo. Valores holgados para no
# estorbar a un cajero con dedos torpes, pero que estrangulan un barrido automatizado.
MAX_FALLOS  = _int_env("LOGIN_MAX_FALLOS", 10)
VENTANA_MIN = _int_env("LOGIN_VENTANA_MIN", 5)
BLOQUEO_MIN = _int_env("LOGIN_BLOQUEO_MIN", 5)


def evaluar(ip: str) -> tuple:
    """(bloqueado: bool, segundos_restantes: int) para esta IP. Bloqueado si acumuló
    MAX_FALLOS o más fallos en los últimos VENTANA_MIN minutos; la espera se cuenta desde el
    fallo más reciente + BLOQUEO_MIN. Ante fallo de BD devuelve (False, 0): no bloquea."""
    ip = (ip or "global")[:64]
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT COUNT(*) AS n, "
                "  CEIL(EXTRACT(EPOCH FROM "
                "    (MAX(creado) + make_interval(mins => :b) - NOW()))) AS espera "
                "FROM login_intentos "
                "WHERE ip = :ip AND creado > NOW() - make_interval(mins => :w)"
            ), {"ip": ip, "b": BLOQUEO_MIN, "w": VENTANA_MIN}).mappings().first()
        n = int(row["n"] or 0)
        espera = int(row["espera"] or 0)
        if n >= MAX_FALLOS and espera > 0:
            return True, espera
        return False, 0
    except Exception:
        return False, 0


def registrar_fallo(ip: str) -> None:
    """Anota un intento fallido de esta IP y hace limpieza oportunista de los muy viejos
    (>1 h; ya no cuentan para ninguna ventana). Best-effort."""
    ip = (ip or "global")[:64]
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO login_intentos (ip) VALUES (:ip)"), {"ip": ip})
            conn.execute(text(
                "DELETE FROM login_intentos WHERE creado < NOW() - INTERVAL '1 hour'"))
    except Exception:
        pass


def limpiar(ip: str) -> None:
    """Borra los fallos acumulados de esta IP tras un login EXITOSO (empieza de cero)."""
    ip = (ip or "global")[:64]
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM login_intentos WHERE ip = :ip"), {"ip": ip})
    except Exception:
        pass
