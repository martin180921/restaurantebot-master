"""Ciclo de vida de los PINs de turno del mesero (acceso efímero).

El mesero ya no usa contraseña fija: en caja se le genera un PIN de turno que solo
funciona mientras esté activo. Se revoca a mano (el mesero termina su turno) o en bloque
al cerrar la caja. El panel revalida el PIN en cada run, así una revocación echa al
mesero ya conectado dentro de un refresco (~30 s) o en su siguiente interacción.

Vive aparte de auth.py (que se mantiene sin dependencias de BD) y comparte el engine
de db.py. Solo se guarda el hash del PIN; el PIN en claro se muestra una vez al crearlo.
"""
import hashlib
import secrets

from sqlalchemy import text

from db import engine


def _hash(pin: str) -> str:
    return hashlib.sha256(str(pin or "").strip().encode()).hexdigest()


def _pin_aleatorio() -> str:
    """PIN numérico de 6 dígitos (fácil de teclear en móvil)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def generar_clave(etiqueta: str = "", creada_por: str = "") -> str | None:
    """Crea un PIN de turno activo y devuelve el PIN EN CLARO (se muestra una sola vez).
    Reintenta si por casualidad colisiona con otro PIN activo. None si falla la BD."""
    etiqueta = (etiqueta or "").strip()[:120] or None
    creada_por = (creada_por or "").strip()[:20] or None
    try:
        with engine.begin() as conn:
            for _ in range(5):
                pin = _pin_aleatorio()
                h = _hash(pin)
                choca = conn.execute(text(
                    "SELECT 1 FROM claves_mesero WHERE clave_hash = :h AND activa = TRUE"
                ), {"h": h}).first()
                if choca:
                    continue
                conn.execute(text(
                    "INSERT INTO claves_mesero (etiqueta, clave_hash, creada_por) "
                    "VALUES (:e, :h, :cp)"
                ), {"e": etiqueta, "h": h, "cp": creada_por})
                return pin
    except Exception:
        return None
    return None


def validar_clave(pin: str) -> int | None:
    """Id de la clave ACTIVA cuyo hash coincide con 'pin', o None. Para el login."""
    pin = (pin or "").strip()
    if not pin:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT id FROM claves_mesero WHERE clave_hash = :h AND activa = TRUE "
                "ORDER BY id DESC LIMIT 1"
            ), {"h": _hash(pin)}).first()
        return int(row[0]) if row else None
    except Exception:
        return None


def clave_activa(key_id) -> bool:
    """True si la clave sigue activa. Lo llama el panel en cada run (revocación inmediata)."""
    if not key_id:
        return False
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT 1 FROM claves_mesero WHERE id = :id AND activa = TRUE"
            ), {"id": int(key_id)}).first()
        return row is not None
    except Exception:
        # Ante un fallo de BD NO echamos al mesero (evita deslogueos por un blip de red);
        # la próxima lectura sana resolverá. La revocación se aplicará en cuanto la BD vuelva.
        return True


def revocar_clave(key_id) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE claves_mesero SET activa = FALSE, revocada = NOW() "
                "WHERE id = :id AND activa = TRUE"
            ), {"id": int(key_id)})
    except Exception:
        pass


def revocar_todas() -> int:
    """Revoca TODAS las claves activas (cierre de caja). Devuelve cuántas se revocaron."""
    try:
        with engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE claves_mesero SET activa = FALSE, revocada = NOW() WHERE activa = TRUE"
            ))
        return res.rowcount or 0
    except Exception:
        return 0


def claves_activas():
    """[{id, etiqueta, creada}] de las claves activas, para listarlas en caja."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, etiqueta, creada FROM claves_mesero WHERE activa = TRUE "
                "ORDER BY id DESC"
            )).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []
