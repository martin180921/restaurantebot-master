"""Libro mayor de auditoría (FASE 1): registro central de eventos críticos.

Una única función de escritura — registrar() — que toda acción sensible del dominio
llama para dejar rastro de QUIÉN hizo QUÉ, CUÁNDO y sobre qué entidad. El actor sale
de la sesión (auth) si no se pasa explícito. Es TOLERANTE A FALLOS a propósito: una
auditoría que falla NUNCA debe tumbar la operación de negocio que la disparó (un cobro
ya commiteado no puede romperse porque el log falle), así que todo va envuelto en
try/except y se traga el error.

Lecturas para los informes (cargar_auditoria, reporte_personal) viven aquí también.
Importa db (engine) y auth (identidad de sesión); ninguno depende de este módulo → sin
ciclos. La tabla la garantiza db._ensure_schema() al importar db.
"""
import json

from sqlalchemy import text

import auth
from db import engine, RESTAURANTE_ID


# ── Escritura ─────────────────────────────────────────────────────────────────────
def registrar(accion: str, entidad: str = None, entidad_id=None, detalle: dict = None,
              actor_nombre: str = None, actor_rol: str = None) -> None:
    """Anota un evento en el libro mayor. Si no se pasa actor, lo toma de la sesión.

    'accion'  → verbo del evento: 'cobrar' | 'cancelar_pedido' | 'descuento' |
                'cortesia' | 'checkout_iniciado' | 'clock_in' | 'clock_out' |
                'empleado_creado' | 'empleado_baja' | 'gasto_caja' | 'base_repartidor'…
    'entidad' → tabla/concepto afectado: 'pedido' | 'empleado' | 'caja' | 'sesion'.
    'detalle' → JSONB con el diff o metadatos (montos, total_antes/después, ids…).
    Nunca lanza: un fallo de auditoría no debe propagarse a la UI ni revertir la acción.
    """
    if actor_nombre is None or actor_rol is None:
        n, r = actor()
        actor_nombre = actor_nombre or n
        actor_rol = actor_rol or r
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO auditoria
                    (actor_nombre, actor_rol, accion, entidad, entidad_id, detalle, restaurante_id)
                VALUES
                    (:an, :ar, :ac, :en, :eid, CAST(:det AS JSONB), :rid)
            """), {
                "an": (actor_nombre or None),
                "ar": (actor_rol or None),
                "ac": str(accion)[:40],
                "en": (str(entidad)[:40] if entidad else None),
                "eid": (int(entidad_id) if entidad_id is not None else None),
                "det": json.dumps(detalle or {}, ensure_ascii=False, default=str),
                "rid": int(RESTAURANTE_ID),
            })
    except Exception:
        # El evento de negocio ya ocurrió; un log fallido no debe romper el flujo.
        pass


# ── Identidad del actor (desde la sesión) ────────────────────────────────────────
def actor() -> tuple:
    """(nombre, rol) del usuario en sesión para estampar en el log. Defaults seguros."""
    try:
        return auth.actor()
    except Exception:
        return ("Desconocido", auth.current_role() or "")


# ── Lecturas para informes ────────────────────────────────────────────────────────
def cargar_auditoria(limite: int = 200, accion: str = None, actor_nombre: str = None):
    """Eventos recientes del libro mayor (más nuevo primero), con filtros opcionales por
    acción y/o actor. Tolerante a fallos: lista vacía si la tabla aún no existe."""
    clausulas, params = [], {"n": int(limite)}
    if accion:
        clausulas.append("accion = :ac")
        params["ac"] = accion
    if actor_nombre:
        clausulas.append("actor_nombre = :an")
        params["an"] = actor_nombre
    where = ("WHERE " + " AND ".join(clausulas)) if clausulas else ""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT id, ts, actor_nombre, actor_rol, accion, entidad, entidad_id, detalle "
                f"FROM auditoria {where} ORDER BY ts DESC LIMIT :n"
            ), params).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


def reporte_personal(desde, hasta) -> list:
    """Informe de actividad POR EMPLEADO en el rango [desde, hasta] (fechas date).

    Agrega el libro mayor por actor: nº y monto de cobros, cancelaciones, descuentos
    (incl. cortesías) con su monto, y horas trabajadas a partir de sesiones_empleado.
    Devuelve [{actor, rol, horas, cobros_n, cobros_monto, cancel_n, desc_n, desc_monto}]
    ordenado por monto cobrado desc. Tolerante a fallos → lista vacía.

    Nota: el monto se lee del JSONB 'detalle' (->>'monto') que cada acción guarda;
    por eso 'cobrar'/'descuento' deben registrar ese campo (lo hacen en pedidos.py).
    """
    try:
        with engine.connect() as conn:
            filas = conn.execute(text("""
                SELECT
                    COALESCE(actor_nombre, 'Desconocido') AS actor,
                    MAX(actor_rol)                        AS rol,
                    COUNT(*) FILTER (WHERE accion = 'cobrar')                       AS cobros_n,
                    COALESCE(SUM((detalle->>'monto')::numeric)
                             FILTER (WHERE accion = 'cobrar'), 0)                   AS cobros_monto,
                    COUNT(*) FILTER (WHERE accion = 'cancelar_pedido')             AS cancel_n,
                    COUNT(*) FILTER (WHERE accion IN ('descuento', 'cortesia'))    AS desc_n,
                    COALESCE(SUM((detalle->>'monto')::numeric)
                             FILTER (WHERE accion IN ('descuento', 'cortesia')), 0) AS desc_monto
                FROM auditoria
                WHERE ts::date BETWEEN :d AND :h
                GROUP BY COALESCE(actor_nombre, 'Desconocido')
            """), {"d": desde, "h": hasta}).mappings().all()
            agg = {r["actor"]: dict(r) for r in filas}

            # Horas trabajadas: suma de (logout_at − login_at) de las sesiones que
            # solapan el rango. Las sesiones aún activas cuentan hasta NOW().
            horas = conn.execute(text("""
                SELECT COALESCE(nombre, 'Desconocido') AS actor,
                       COALESCE(SUM(EXTRACT(EPOCH FROM
                           (COALESCE(logout_at, NOW()) - login_at)) / 3600.0), 0) AS horas
                FROM sesiones_empleado
                WHERE login_at::date BETWEEN :d AND :h
                GROUP BY COALESCE(nombre, 'Desconocido')
            """), {"d": desde, "h": hasta}).mappings().all()
            horas_por_actor = {r["actor"]: float(r["horas"] or 0) for r in horas}
    except Exception:
        return []

    # Une ambos lados (puede haber quien cobró sin sesión registrada o viceversa).
    actores = set(agg) | set(horas_por_actor)
    out = []
    for a in actores:
        base = agg.get(a, {})
        out.append({
            "actor":        a,
            "rol":          base.get("rol") or "",
            "horas":        round(horas_por_actor.get(a, 0.0), 1),
            "cobros_n":     int(base.get("cobros_n", 0) or 0),
            "cobros_monto": int(base.get("cobros_monto", 0) or 0),
            "cancel_n":     int(base.get("cancel_n", 0) or 0),
            "desc_n":       int(base.get("desc_n", 0) or 0),
            "desc_monto":   int(base.get("desc_monto", 0) or 0),
        })
    out.sort(key=lambda r: r["cobros_monto"], reverse=True)
    return out
