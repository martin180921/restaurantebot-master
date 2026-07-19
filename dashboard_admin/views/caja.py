"""Vista de Caja: cierre de caja por turno (arqueo con apertura y conteo).

Un turno se abre con un 'monto de apertura' (base en efectivo). Durante el turno,
los cobros del libro 'pagos' se acumulan por método. Al cerrar se congela el arqueo:
    total_esperado = monto_apertura + efectivo_esperado   (lo que debe haber en caja)
    diferencia     = efectivo_real − total_esperado        (sobrante / faltante)
También se registran las transferencias verificadas en banco (transferencia_real)
para contrastarlas con lo esperado, aunque NO entran a la caja física de efectivo.
Hay como máximo un turno con estado='abierto' a la vez.
"""
import streamlit as st
from sqlalchemy import text, bindparam
import json
import html

import auth
import audit
import empleados
import mesero_keys
from db import engine, fmt_money, flash, saldo_pedido, titulo_seccion
from utils.print_jobs import (badge_agente_html, enqueue_hoja_ruta, enqueue_recibo,
                              enqueue_abrir_cajon)
from views import pedidos, menu


# ── DB ───────────────────────────────────────────────────────────────────────────
def cierre_activo():
    """El turno con estado='abierto' como dict, o None. Tolerante a fallos."""
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT id, fecha_apertura, monto_apertura FROM cierres_caja "
                "WHERE estado = 'abierto' ORDER BY id DESC LIMIT 1"
            )).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None


def ingresos_esperados(fecha_apertura) -> dict:
    """{efectivo, transferencia} cobrados desde 'fecha_apertura' (libro 'pagos').

    efectivo_esperado      = SUM(monto) WHERE metodo='efectivo'      AND fecha >= apertura
    transferencia_esperada = SUM(monto) WHERE metodo='transferencia' AND fecha >= apertura
    Tolerante a fallos → ceros si la tabla aún no existe.
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT metodo, COALESCE(SUM(monto), 0) AS total FROM pagos "
                "WHERE fecha >= :ini GROUP BY metodo"
            ), {"ini": fecha_apertura}).mappings().all()
        por_metodo = {r["metodo"]: int(r["total"]) for r in rows}
    except Exception:
        por_metodo = {}
    return {
        "efectivo": por_metodo.get("efectivo", 0),
        "transferencia": por_metodo.get("transferencia", 0),
    }


def abrir_caja(monto_apertura: int) -> bool:
    """Abre un turno si no hay otro con estado='abierto' (guard atómico)."""
    # Candado de capacidad (RBAC): solo admin/caja gestionan el arqueo.
    if not auth.can("manage_caja"):
        return False
    nuevo_id = None
    with engine.begin() as conn:
        existe = conn.execute(text(
            "SELECT 1 FROM cierres_caja WHERE estado = 'abierto' LIMIT 1"
        )).first()
        if existe:
            return False
        nuevo_id = conn.execute(text(
            "INSERT INTO cierres_caja (monto_apertura, estado) VALUES (:m, 'abierto') RETURNING id"
        ), {"m": int(monto_apertura)}).scalar_one()
    audit.registrar("caja_apertura", "caja", int(nuevo_id) if nuevo_id else None,
                    {"monto_apertura": int(monto_apertura)})
    return True


def cerrar_caja(cierre_id: int, efectivo_esperado: int, transferencia_esperada: int,
                efectivo_real: int, transferencia_real: int, diferencia: int):
    """Congela el arqueo y cierra el turno (estado='cerrado', fecha_cierre=NOW())."""
    # Candado de capacidad (RBAC): solo admin/caja gestionan el arqueo.
    if not auth.can("manage_caja"):
        return
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE cierres_caja SET "
            "  fecha_cierre = NOW(), "
            "  efectivo_esperado = :ee, transferencia_esperada = :te, "
            "  efectivo_real = :er, transferencia_real = :tr, "
            "  diferencia = :dif, estado = 'cerrado' "
            "WHERE id = :id AND estado = 'abierto'"
        ), {
            "ee": int(efectivo_esperado), "te": int(transferencia_esperada),
            "er": int(efectivo_real), "tr": int(transferencia_real),
            "dif": int(diferencia), "id": int(cierre_id),
        })
    # Cierre de caja = fin de jornada → barrido de acceso de meseros: revoca los PINs de
    # turno EFÍMEROS, BLOQUEA el PIN de los meseros con perfil, y marca la SALIDA de todo el
    # personal (clock-out masivo) EXCEPTO el propio cajero que cierra. Cada mesero conectado
    # se desloguea en su próximo run; ningún mesero vuelve a entrar hasta que se reactive.
    mesero_keys.revocar_todas()
    empleados.bloquear_meseros()
    cerradas = empleados.cerrar_todas_sesiones(excepto=st.session_state.get("sesion_id"))
    audit.registrar("caja_cierre", "caja", int(cierre_id),
                    {"efectivo_real": int(efectivo_real),
                     "transferencia_real": int(transferencia_real),
                     "diferencia": int(diferencia), "sesiones_cerradas": int(cerradas)})


def cierres_recientes(n: int = 8):
    """Últimos turnos cerrados para el historial. Tolerante a fallos."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, fecha_apertura, fecha_cierre, monto_apertura, "
                "       efectivo_esperado, transferencia_esperada, "
                "       efectivo_real, transferencia_real, diferencia "
                "FROM cierres_caja WHERE estado = 'cerrado' ORDER BY id DESC LIMIT :n"
            ), {"n": n}).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── DB: flujo de caja (movimientos de efectivo del cajón) ───────────────────────
# Cada fila de movimientos_caja es un evento de efectivo que YA ocurrió (el dinero ya
# salió o entró), así que el neto cuenta TODAS las filas del turno sin importar
# 'estado'; 'estado' solo marca cuáles aún esperan su retorno (devolución / depósito).
#   gasto           → efectivo SALE del cajón (−)
#   reingreso_gasto → vuelve el cambio del gasto (+)
#   base_repartidor → efectivo SALE como base de cambio del repartidor (−)
#   retorno_base    → el repartidor devuelve las vueltas sobrantes al volver (+)
# Los COBROS de los pedidos de domicilio NO entran aquí: se cobran al volver por el
# libro 'pagos' (ventas en efectivo del arqueo), así no se cuentan dos veces.
TIPOS_SALIDA  = ("gasto", "base_repartidor")
TIPOS_ENTRADA = ("reingreso_gasto", "retorno_base")


def _actor_rol() -> str:
    return auth.current_role() or ""


def _actor_nombre() -> str:
    """Nombre del operador en sesión (para el libro mayor)."""
    n, _ = audit.actor()
    return n


def registrar_gasto(cierre_id: int, monto: int, motivo: str, actor_nombre: str = None):
    """Retiro de efectivo del cajón (gasto/imprevisto). Queda 'abierto' hasta que se
    registre la devolución del cambio sobrante."""
    if not auth.can("manage_caja"):
        return
    # 'actor_nombre' = quién retira (puede ser un tercero); si va vacío, el operador en sesión.
    quien = (actor_nombre or "").strip() or _actor_nombre()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO movimientos_caja (cierre_id, tipo, monto, motivo, actor_rol, "
            "actor_nombre, estado) VALUES (:c, 'gasto', :m, :mo, :ar, :an, 'abierto')"
        ), {"c": int(cierre_id), "m": int(monto), "mo": (motivo or "").strip() or None,
            "ar": _actor_rol(), "an": quien})
    audit.registrar("gasto_caja", "caja", int(cierre_id),
                    {"monto": int(monto), "motivo": (motivo or "").strip() or None,
                     "retira": quien})


def registrar_reingreso(gasto_id: int, monto: int):
    """Devuelve al cajón el cambio sobrante de un gasto y lo marca 'cerrado'. Atómico."""
    if not auth.can("manage_caja"):
        return
    cid = None
    with engine.begin() as conn:
        ref = conn.execute(text(
            "SELECT cierre_id FROM movimientos_caja WHERE id = :id AND tipo = 'gasto' "
            "AND estado = 'abierto' FOR UPDATE"
        ), {"id": int(gasto_id)}).mappings().first()
        if not ref:
            return
        cid = ref["cierre_id"]
        if int(monto) > 0:
            conn.execute(text(
                "INSERT INTO movimientos_caja (cierre_id, tipo, monto, ref_id, actor_rol, "
                "estado) VALUES (:c, 'reingreso_gasto', :m, :ref, :ar, 'cerrado')"
            ), {"c": cid, "m": int(monto), "ref": int(gasto_id), "ar": _actor_rol()})
        conn.execute(text("UPDATE movimientos_caja SET estado = 'cerrado' WHERE id = :id"),
                     {"id": int(gasto_id)})
    audit.registrar("reingreso_gasto", "caja", int(cid) if cid is not None else None,
                    {"gasto_id": int(gasto_id), "monto": int(monto)})


class _BaseConflict(Exception):
    """Señal interna (H1): la base no se pudo conciliar — un pedido ya fue tomado por otra
    base / cobrado, o la base aún tiene saldo por cobrar. Revienta el txn de engine.begin()
    → ROLLBACK total (ni base ni enlaces ni cierre)."""


def registrar_base_repartidor(cierre_id: int, monto: int, nombre: str, pedidos_ids=None) -> tuple:
    """Saca efectivo del cajón como base de cambio para un repartidor y ENLAZA los pedidos
    de domicilio que se lleva (pedidos.base_id) para conciliarlos a su regreso. Devuelve
    (ok, mensaje).

    H1 — enlace atómico y exclusivo: el INSERT de la base y el UPDATE CONDICIONAL de los
    pedidos (base_id IS NULL AND pagado=FALSE AND no cancelado) viven en la MISMA transacción.
    Si entre la selección y el envío algún pedido ya entró a otra base o se cobró, el rowcount
    no cuadra con lo pedido → ROLLBACK total y se pide reintentar. Así un mismo pedido NUNCA
    queda asignado a dos bases. 'pedidos_ref' (JSON) se conserva solo para la vista/legado."""
    if not auth.can("manage_caja"):
        return False, "Sin permiso para gestionar caja."
    ids = [int(i) for i in (pedidos_ids or [])]
    base_id = None
    try:
        with engine.begin() as conn:
            base_id = conn.execute(text(
                "INSERT INTO movimientos_caja (cierre_id, tipo, monto, actor_rol, actor_nombre, "
                "pedidos_ref, estado) VALUES (:c, 'base_repartidor', :m, :ar, :an, :pr, 'abierto') "
                "RETURNING id"
            ), {"c": int(cierre_id), "m": int(monto), "ar": _actor_rol(),
                "an": (nombre or "").strip() or None,
                "pr": (json.dumps(ids) if ids else None)}).scalar_one()
            if ids:
                upd = text(
                    "UPDATE pedidos SET base_id = :bid "
                    "WHERE id IN :ids AND base_id IS NULL AND pagado = FALSE "
                    "AND estado <> 'cancelado'"
                ).bindparams(bindparam("ids", expanding=True))
                n = conn.execute(upd, {"bid": int(base_id), "ids": ids}).rowcount or 0
                if n != len(ids):
                    raise _BaseConflict()   # revienta el txn → ni base ni enlaces
    except _BaseConflict:
        return False, ("Uno o más pedidos ya fueron tomados por otra base o cobrados. "
                       "Vuelve a abrir la base y elígelos de nuevo.")
    audit.registrar("base_repartidor", "caja", int(cierre_id),
                    {"monto": int(monto), "repartidor": (nombre or "").strip() or None,
                     "pedidos": ids, "base_id": int(base_id) if base_id is not None else None})
    # E3 — hoja de ruta del repartidor (paradas + dirección + a cobrar). Tras el commit y
    # best-effort: si falla la impresión, la base ya quedó registrada (se audita el fallo).
    if ids and base_id is not None:
        enqueue_hoja_ruta(int(base_id), nombre, int(monto), ids)
    return True, "ok"


def registrar_retorno_base(base_id: int, monto: int, entregado: int = None,
                           esperado: int = None) -> tuple:
    """Devuelve al cajón las vueltas sobrantes de una base de repartidor y la cierra.
    Devuelve (ok, mensaje). Los pedidos ya se cobraron por separado (libro 'pagos'); aquí
    solo vuelve el cambio que no se usó.

    'entregado'/'esperado' (opcionales): la liquidación en efectivo que el cajero verificó
    contra el repartidor (ver _dialog_retorno) — se anotan en la auditoría como rastro de la
    conciliación, aunque el monto asentado en el cajón sigue siendo 'monto' (las vueltas).

    H1 — no se cierra con cobros pendientes: dentro del MISMO txn (FOR UPDATE sobre la base)
    se suma el saldo de los pedidos enlazados (base_id). Si es > 0, el repartidor volvió con
    cobros sin registrar → ROLLBACK y se rechaza el cierre (evita descuadrar la caja). Al
    cerrar OK se CONSERVA base_id en los pedidos (historial de qué repartidor entregó cada uno)."""
    if not auth.can("manage_caja"):
        return False, "Sin permiso para gestionar caja."
    cid = None
    try:
        with engine.begin() as conn:
            ref = conn.execute(text(
                "SELECT cierre_id FROM movimientos_caja WHERE id = :id "
                "AND tipo = 'base_repartidor' AND estado = 'abierto' FOR UPDATE"
            ), {"id": int(base_id)}).mappings().first()
            if not ref:
                return False, "La base ya estaba cerrada o no existe."
            cid = ref["cierre_id"]
            # Fuente de verdad del saldo pendiente: los pedidos enlazados por base_id (no el
            # JSON pedidos_ref). Si algo sigue sin cobrar, no se deja cerrar la base.
            saldo_pend = conn.execute(text(
                "SELECT COALESCE(SUM(total - COALESCE(total_pagado, 0)), 0) FROM pedidos "
                "WHERE base_id = :bid AND estado <> 'cancelado' AND pagado = FALSE"
            ), {"bid": int(base_id)}).scalar_one() or 0
            if int(saldo_pend) > 0:
                raise _BaseConflict()
            if int(monto) > 0:
                conn.execute(text(
                    "INSERT INTO movimientos_caja (cierre_id, tipo, monto, ref_id, actor_rol, "
                    "estado) VALUES (:c, 'retorno_base', :m, :ref, :ar, 'cerrado')"
                ), {"c": cid, "m": int(monto), "ref": int(base_id), "ar": _actor_rol()})
            conn.execute(text("UPDATE movimientos_caja SET estado = 'cerrado' WHERE id = :id"),
                         {"id": int(base_id)})
    except _BaseConflict:
        return False, ("Aún hay pedidos de esta base sin cobrar. Cóbralos en 💵 Cobrar antes "
                       "de cerrar la base (evita descuadrar la caja).")
    detalle = {"base_id": int(base_id), "monto": int(monto)}
    if entregado is not None:
        detalle["entregado"] = int(entregado)
    if esperado is not None:
        detalle["esperado"] = int(esperado)
    if entregado is not None and esperado is not None:
        detalle["diferencia"] = int(entregado) - int(esperado)
    audit.registrar("retorno_base", "caja", int(cid) if cid is not None else None, detalle)
    return True, "ok"


# ── Vueltas: cuánto cambio necesita el repartidor por cada pedido ───────────────
def _vueltas_pedido(saldo: int, metodo, paga_con) -> int:
    """Cambio (vueltas) que el repartidor debe llevar para ESTE pedido: solo aplica si paga
    en efectivo y el cliente indicó con cuánto paga (paga_con > saldo). Transferencia, pago
    ya cubierto, o sin dato de paga_con → 0 vueltas (nada que cambiar en la puerta)."""
    saldo = max(0, int(saldo or 0))
    paga_con = int(paga_con or 0)
    if (metodo or "efectivo") != "efectivo" or paga_con <= 0:
        return 0
    return max(0, paga_con - saldo)


def _base_sugerida(vueltas_total: int) -> int:
    """Redondea la suma de vueltas necesarias HACIA ARRIBA al múltiplo de $5.000 más cercano
    (billetes/monedas manejables). 0 → 0 (no forzar una base si nadie necesita cambio)."""
    v = max(0, int(vueltas_total or 0))
    if v <= 0:
        return 0
    return -(-v // 5000) * 5000


def _vueltas_devueltas(entregado: int, efectivo_cobrado: int, base_monto: int) -> int:
    """Cuánto de lo que el repartidor pone sobre el mostrador son VUELTAS que vuelven al
    cajón (el resto ya se registró como cobro en 'pagos'). El repartidor entrega UN solo
    fajo (base + lo cobrado en efectivo en la puerta); el cajero solo cuenta ese fajo, y de
    aquí se deriva la partición — nunca se le pide partirlo a él. Acotado a [0, base_monto]:
    ni negativo (si entregó menos de lo cobrado) ni más de lo que se llevó de base."""
    return max(0, min(int(base_monto or 0),
                      int(entregado or 0) - int(efectivo_cobrado or 0)))


# ── Pedidos de domicilio para el flujo del repartidor ───────────────────────────
def pedidos_domicilio_pendientes():
    """[{id, nombre, saldo, metodo, paga_con, vueltas}] de pedidos de domicilio/para_llevar
    de HOY, con saldo y que AÚN no van en una base, para asignarlos a la base de un
    repartidor. 'vueltas' es el cambio que ese pedido exige en la puerta (ver
    _vueltas_pedido) — permite sugerir la base sin que el cajero calcule de cabeza.
    Tolerante a fallos.

    Filtros (cada uno corrige un problema real):
      · fecha::date = CURRENT_DATE → solo los de HOY. CURRENT_DATE es hora de Bogotá (la
        conexión fija timezone=America/Bogota en db.py); sin este filtro se arrastraban los
        pedidos de ayer que quedaron sin cobrar/sin marcar → aparecían todos en el selector.
      · base_id IS NULL → no re-ofrecer un pedido que ya va con otro repartidor (H1).
    Mantiene estado<>'cancelado' y pagado=FALSE para no excluir un domicilio 'listo' o ya
    'entregado' que todavía está por cobrar (el cobro del domicilio ocurre al volver)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, numero_cliente, cliente_nombre, total,
                       COALESCE(total_pagado, 0) AS total_pagado, pagado,
                       metodo_pago, COALESCE(paga_con, 0) AS paga_con
                FROM pedidos
                WHERE tipo_entrega IN ('domicilio', 'para_llevar')
                  AND estado <> 'cancelado' AND pagado = FALSE
                  AND base_id IS NULL
                  AND fecha::date = CURRENT_DATE
                ORDER BY id
            """)).mappings().all()
    except Exception:
        return None   # None = la LECTURA falló (≠ [] = no hay pendientes) → no crear base a ciegas
    out = []
    for r in rows:
        d = dict(r)
        s = saldo_pedido(d)
        if s > 0:
            out.append({"id": int(d["id"]),
                        "nombre": d.get("cliente_nombre") or d.get("numero_cliente") or f"#{d['id']}",
                        "saldo": s, "metodo": d.get("metodo_pago") or "efectivo",
                        "paga_con": int(d.get("paga_con") or 0),
                        "vueltas": _vueltas_pedido(s, d.get("metodo_pago"), d.get("paga_con"))})
    return out


def _cuentas_por_cobrar_hoy():
    """[{tipo, nombre, ids, saldo}] de las cuentas de HOY con saldo pendiente, para el
    punto de entrada 'Cobrar mesa' de la caja simple. Agrupa por mesa (mesa_id) cuando
    aplica; los pedidos de entrega sin mesa se listan uno a uno (si YA tienen una base de
    repartidor asignada, se cobran desde 🛵 Repartidores, no aquí, para no ofrecer el
    mismo pedido por dos caminos). Los pedidos heredados sin mesa_id (previos a esa
    columna) caen como líneas sueltas en vez de agruparse por nombre de mesa — caso raro,
    se sigue pudiendo cobrar, solo no se agrupa. Tolerante a fallos → None (no listar
    nada si la lectura falló, para no dar una foto a medias)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT p.id, p.mesa_id, m.nombre AS mesa_nombre, p.numero_cliente,
                       p.cliente_nombre, p.total, COALESCE(p.total_pagado, 0) AS total_pagado,
                       p.base_id
                FROM pedidos p
                LEFT JOIN mesas m ON m.id = p.mesa_id
                WHERE p.pagado = FALSE AND p.estado <> 'cancelado'
                  AND p.fecha::date = CURRENT_DATE
                ORDER BY COALESCE(p.mesa_id, 999999), p.id
            """)).mappings().all()
    except Exception:
        return None
    mesas_map, sueltos = {}, []
    for r in rows:
        d = dict(r)
        s = saldo_pedido(d)
        if s <= 0:
            continue
        if d.get("mesa_id") is not None:
            mid = int(d["mesa_id"])
            g = mesas_map.setdefault(mid, {
                "tipo": "mesa", "nombre": d.get("mesa_nombre") or f"Mesa {mid}",
                "ids": [], "saldo": 0,
            })
            g["ids"].append(int(d["id"]))
            g["saldo"] += s
        elif d.get("base_id") is None:
            sueltos.append({
                "tipo": "pedido",
                "nombre": d.get("cliente_nombre") or d.get("numero_cliente") or f"#{d['id']}",
                "ids": [int(d["id"])], "saldo": s,
            })
    return list(mesas_map.values()) + sueltos


def _cuentas_cobradas_hoy():
    """[{tipo, nombre, ids, total}] de las cuentas que ya se cobraron por completo y se
    PAGARON hoy (fecha de 'pagos', no de creación del pedido: un domicilio armado ayer y
    cobrado hoy en la puerta debe listarse igual). Sin esto, 'Reimprimir recibo' —que
    solo vive en la rama total<=0 de pedidos.dialog_cobrar— era prácticamente
    inalcanzable: ninguna otra pantalla ofrece una cuenta ya saldada. Agrupa por mesa
    igual que _cuentas_por_cobrar_hoy. Tolerante a fallos → None."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT p.id, p.mesa_id, m.nombre AS mesa_nombre, p.numero_cliente,
                       p.cliente_nombre, p.total
                FROM pedidos p
                JOIN pagos pg ON pg.pedido_id = p.id
                LEFT JOIN mesas m ON m.id = p.mesa_id
                WHERE p.pagado = TRUE AND pg.fecha::date = CURRENT_DATE
                ORDER BY p.id DESC
            """)).mappings().all()
    except Exception:
        return None
    mesas_map, sueltos = {}, []
    for r in rows:
        d = dict(r)
        if d.get("mesa_id") is not None:
            mid = int(d["mesa_id"])
            g = mesas_map.setdefault(mid, {
                "tipo": "mesa", "nombre": d.get("mesa_nombre") or f"Mesa {mid}",
                "ids": [], "total": 0,
            })
            g["ids"].append(int(d["id"]))
            g["total"] += int(d["total"] or 0)
        else:
            sueltos.append({
                "tipo": "pedido",
                "nombre": d.get("cliente_nombre") or d.get("numero_cliente") or f"#{d['id']}",
                "ids": [int(d["id"])], "total": int(d["total"] or 0),
            })
    return list(mesas_map.values()) + sueltos


def pedidos_de_base(base_id: int):
    """[{id, nombre, total, cobrado, saldo, pagado, metodo_pago, paga_con, vueltas}] de los
    pedidos ENLAZADOS a una base de repartidor por pedidos.base_id (H1) — la fuente de
    verdad del flujo del repartidor, en vez del JSON pedidos_ref. Excluye cancelados.
    'vueltas' es el cambio que ESTE pedido exige en la puerta (0 si ya está cobrado, es
    transferencia, o el cliente no dio paga_con). Tolerante a fallos."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, numero_cliente, cliente_nombre, total, "
                "       COALESCE(total_pagado, 0) AS total_pagado, pagado, metodo_pago, "
                "       COALESCE(paga_con, 0) AS paga_con "
                "FROM pedidos WHERE base_id = :bid AND estado <> 'cancelado' ORDER BY id"
            ), {"bid": int(base_id)}).mappings().all()
    except Exception:
        return None   # None = la LECTURA falló (≠ [] = base sin pedidos) → no conciliar a ciegas
    out = []
    for r in rows:
        d = dict(r)
        saldo = saldo_pedido(d)
        out.append({"id": int(d["id"]),
                    "nombre": d.get("cliente_nombre") or d.get("numero_cliente") or f"#{d['id']}",
                    "total": int(d["total"] or 0), "cobrado": int(d["total_pagado"] or 0),
                    "saldo": saldo, "pagado": bool(d["pagado"]),
                    "metodo_pago": d.get("metodo_pago"), "paga_con": int(d.get("paga_con") or 0),
                    "vueltas": _vueltas_pedido(saldo, d.get("metodo_pago"), d.get("paga_con"))})
    return out


def efectivo_cobrado_de_base(base_id: int) -> int:
    """Σ de los abonos en EFECTIVO (libro 'pagos') de los pedidos enlazados a esta base —
    lo que el repartidor debió recibir en efectivo en la puerta. Con la base entregada
    permite cuadrar el efectivo que trae de vuelta (E4). Tolerante a fallos (0)."""
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(
                "SELECT COALESCE(SUM(pg.monto), 0) FROM pagos pg "
                "JOIN pedidos p ON p.id = pg.pedido_id "
                "WHERE p.base_id = :bid AND pg.metodo = 'efectivo'"
            ), {"bid": int(base_id)}).scalar() or 0)
    except Exception:
        return 0


def movimientos_del_turno(cierre_id: int):
    """Todos los movimientos del turno (recientes primero). Tolerante a fallos."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, tipo, monto, motivo, actor_rol, actor_nombre, ref_id, estado, "
                "       creado_at FROM movimientos_caja WHERE cierre_id = :c "
                "ORDER BY id DESC"
            ), {"c": int(cierre_id)}).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


def movimientos_abiertos(cierre_id: int, tipo: str):
    """Movimientos de un 'tipo' aún 'abiertos' (esperan devolución / retorno)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, monto, motivo, actor_nombre, pedidos_ref, creado_at "
                "FROM movimientos_caja "
                "WHERE cierre_id = :c AND tipo = :t AND estado = 'abierto' ORDER BY id"
            ), {"c": int(cierre_id), "t": tipo}).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


def neto_movimientos(cierre_id: int) -> int:
    """Efecto neto de los movimientos sobre el efectivo del cajón:
    Σ(entradas) − Σ(salidas). Tolerante a fallos → 0."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT tipo, COALESCE(SUM(monto), 0) AS total FROM movimientos_caja "
                "WHERE cierre_id = :c GROUP BY tipo"
            ), {"c": int(cierre_id)}).mappings().all()
        por_tipo = {r["tipo"]: int(r["total"]) for r in rows}
    except Exception:
        return 0
    entra = sum(por_tipo.get(t, 0) for t in TIPOS_ENTRADA)
    sale  = sum(por_tipo.get(t, 0) for t in TIPOS_SALIDA)
    return entra - sale


# ── Helpers de formato ───────────────────────────────────────────────────────────
def _hora(dt) -> str:
    try:
        return dt.strftime("%H:%M")
    except Exception:
        return "—"


def _fechahora(dt) -> str:
    try:
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return "—"


def _metric(col, valor_html: str, label: str, clase: str = ""):
    col.markdown(
        f'<div class="metric-card"><div class="metric-value {clase}" '
        f'style="font-size:clamp(0.9rem,1.6vw,2rem); white-space:nowrap;">{valor_html}</div>'
        f'<div class="metric-label">{label}</div></div>',
        unsafe_allow_html=True,
    )


def _pill(bg: str, borde: str, color: str, texto: str):
    """Píldora de estado (cuadrada / faltante / sobrante)."""
    st.markdown(
        f'<div style="background:{bg}; border:1px solid {borde}; border-radius:10px; '
        f'padding:10px 14px; color:{color}; font-weight:600;">{texto}</div>',
        unsafe_allow_html=True,
    )


# ── Modales de flujo de caja (@st.dialog, no perturban mesas abiertas) ──────────
@st.dialog("🧾 Gasto de caja")
def _dialog_gasto(cierre_id: int):
    st.markdown("Retira efectivo del cajón. Al volver, registra el cambio sobrante.")
    monto = int(st.number_input("Monto a retirar ($)", min_value=0, value=0, step=1000,
                                format="%d", key="gasto_monto") or 0)
    motivo = st.text_input("Motivo / justificación", key="gasto_motivo",
                           placeholder="Ej: compra de gas, insumos urgentes…")
    quien = st.text_input("¿Quién retira? (opcional)", key="gasto_quien")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🧾 Registrar gasto", key="gasto_confirm", type="primary",
                     use_container_width=True, disabled=(monto <= 0 or not motivo.strip())):
            registrar_gasto(cierre_id, monto, motivo, quien)
            flash(f"Gasto registrado · ${fmt_money(monto)}", "🧾")
            st.rerun()
    with c2:
        if st.button("Volver", key="gasto_volver", use_container_width=True):
            st.rerun()


@st.dialog("↩️ Devolución de cambio")
def _dialog_reingreso(gasto_id: int, gasto_monto: int, motivo: str = ""):
    detalle = f" · {html.escape(motivo)}" if motivo else ""
    st.markdown(f"Gasto de **${fmt_money(gasto_monto)}**{detalle}.")
    monto = int(st.number_input("Cambio devuelto al cajón ($)", min_value=0,
                                max_value=int(gasto_monto), value=0, step=1000,
                                format="%d", key=f"reing_monto_{gasto_id}") or 0)
    st.caption(f"Gasto neto: ${fmt_money(int(gasto_monto) - monto)}")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("↩️ Registrar devolución", key=f"reing_confirm_{gasto_id}",
                     type="primary", use_container_width=True):
            registrar_reingreso(gasto_id, monto)   # monto 0 = no sobró nada; cierra el gasto
            flash(f"Devolución registrada · ${fmt_money(monto)}", "↩️")
            st.rerun()
    with c2:
        if st.button("Volver", key=f"reing_volver_{gasto_id}", use_container_width=True):
            st.rerun()


@st.dialog("🛵 Base de repartidor")
def _dialog_base(cierre_id: int):
    st.markdown("Elige los pedidos que lleva el repartidor: el sistema sugiere la base de "
                "cambio que necesita. Al volver, esos pedidos se cobran y devuelve las "
                "vueltas sobrantes.")
    nombre = st.text_input("Nombre del repartidor", key="base_nombre",
                           placeholder="Ej: Carlos")

    pendientes = pedidos_domicilio_pendientes()
    if pendientes is None:
        st.error("⚠️ No se pudieron leer los pedidos de domicilio pendientes (problema de "
                 "conexión). No entregues la base ahora: se crearía SIN pedidos enlazados y no "
                 "se podrían conciliar al volver. Cierra y reintenta en unos segundos.")
        if st.button("Volver", key="base_volver_err", use_container_width=True):
            st.rerun()
        return

    ids = []
    vueltas_total = 0
    if pendientes:
        def _etiqueta(p) -> str:
            if p["vueltas"] > 0:
                return (f"#{p['id']} · {p['nombre']} · ${fmt_money(p['saldo'])} · "
                        f"paga con ${fmt_money(p['paga_con'])} → vueltas ${fmt_money(p['vueltas'])}")
            if (p["metodo"] or "efectivo") != "efectivo":
                return f"#{p['id']} · {p['nombre']} · ${fmt_money(p['saldo'])} · transferencia (sin vueltas)"
            return f"#{p['id']} · {p['nombre']} · ${fmt_money(p['saldo'])}"

        opciones = {_etiqueta(p): p for p in pendientes}
        elegidos = st.multiselect("Pedidos que lleva (se cobran al volver)",
                                  list(opciones.keys()), key="base_pedidos")
        seleccion = [opciones[l] for l in elegidos]
        ids = [p["id"] for p in seleccion]
        vueltas_total = sum(p["vueltas"] for p in seleccion)
    else:
        st.caption("No hay pedidos de domicilio pendientes para asignar (puedes entregar "
                   "la base igual).")

    # A — Base sugerida reactiva: si la selección de pedidos cambió desde el último run,
    # recalculamos la sugerencia y la escribimos en el widget ANTES de instanciarlo (patrón
    # de valor controlado en Streamlit). Si el cajero YA editó el monto a mano y la selección
    # no cambió, su edición se respeta (no se pisa en cada rerun).
    sel_actual = tuple(sorted(ids))
    sugerida = _base_sugerida(vueltas_total)
    if st.session_state.get("_base_prev_sel") != sel_actual:
        st.session_state["_base_prev_sel"] = sel_actual
        st.session_state["base_monto"] = sugerida

    # Caja de sugerencia en HTML (no st.info): Streamlit interpreta '$…$' como fórmula
    # LaTeX y partía el mensaje; con HTML + texto oscuro se lee claro y con contraste.
    if vueltas_total > 0:
        n_txt = "1 pedido" if len(ids) == 1 else f"{len(ids)} pedidos"
        st.markdown(
            f'<div style="background:#eef2ff; border:1px solid #c7d2fe; border-radius:10px; '
            f'padding:12px 14px; margin:6px 0; color:#1e1b4b; line-height:1.5;">'
            f'El repartidor necesita <b>${fmt_money(vueltas_total)}</b> en vueltas para {n_txt}.'
            f'<br><span style="font-size:1.05rem;">Base sugerida: '
            f'<b>${fmt_money(sugerida)}</b></span> '
            f'<span style="color:#4b43b0;">(puedes cambiarla abajo)</span></div>',
            unsafe_allow_html=True)
    elif ids:
        st.caption("Ninguno de los pedidos elegidos necesita vueltas (pago exacto o "
                   "transferencia).")

    monto = int(st.number_input("Base de cambio que se lleva ($)", min_value=0,
                                step=1000, format="%d", key="base_monto") or 0)

    c1, c2 = st.columns(2)
    with c1:
        if st.button("🛵 Entregar base", key="base_confirm", type="primary",
                     use_container_width=True, disabled=(monto <= 0 or not nombre.strip())):
            ok, msg = registrar_base_repartidor(cierre_id, monto, nombre, ids)
            if ok:
                extra = f" · {len(ids)} pedido(s)" if ids else ""
                flash(f"Base entregada · {nombre} · ${fmt_money(monto)}{extra}", "🛵")
                st.rerun()
            else:
                # Conflicto de concurrencia: el modal queda abierto y al reintentar la lista
                # de pendientes se relee (la función no es cacheada) ya sin los conflictivos.
                st.error(msg)
    with c2:
        if st.button("Volver", key="base_volver", use_container_width=True):
            st.rerun()


@st.dialog("🟢 Cerrar base · devolver vueltas")
def _dialog_retorno(base_id: int, base_monto: int, nombre: str = "", pendientes_saldo: int = 0):
    quien = f"**{html.escape(nombre)}** · " if nombre else ""
    st.markdown(f"{quien}base entregada **${fmt_money(base_monto)}**.")
    # Conciliación en vivo desde base_id (no confiamos en el saldo que venía de la tarjeta).
    ordenes = pedidos_de_base(int(base_id))
    if ordenes is None:
        st.error("⚠️ No se pudieron leer los pedidos de esta base (conexión). No cierres la "
                 "base ahora para no descuadrar; reintenta en unos segundos.")
        if st.button("Volver", key=f"ret_err_{base_id}", use_container_width=True):
            st.rerun()
        return
    cobrado = sum(o["cobrado"] for o in ordenes)
    saldo = sum(o["saldo"] for o in ordenes)
    if ordenes:
        color = "#16a34a" if saldo == 0 else "#dc2626"
        st.markdown(
            f'<div style="font-size:0.85rem; color:#45443e;">{len(ordenes)} pedido(s) · '
            f'cobrado <b>${fmt_money(cobrado)}</b> · '
            f'por cobrar <b style="color:{color};">${fmt_money(saldo)}</b></div>',
            unsafe_allow_html=True)
    if saldo > 0:
        st.warning(f"Aún hay ${fmt_money(saldo)} sin cobrar en sus pedidos. Cóbralos antes de "
                   "cerrar para no descuadrar la caja (el cierre se rechazará).")
        c1, c2 = st.columns(2)
        with c1:
            st.button("🟢 Cerrar base", key=f"ret_confirm_{base_id}", type="primary",
                     use_container_width=True, disabled=True)
        with c2:
            if st.button("Volver", key=f"ret_volver_{base_id}", use_container_width=True):
                st.rerun()
        return

    # B — El repartidor entrega UN solo fajo de efectivo (vueltas sobrantes + lo cobrado en
    # la puerta): el cajero solo cuenta ese fajo y teclea UN número. El sistema deriva cuánto
    # de eso son vueltas que vuelven al cajón (el resto ya quedó asentado como cobro en 'pagos').
    efectivo_cobrado = efectivo_cobrado_de_base(int(base_id))
    esperado_vuelta = int(base_monto) + int(efectivo_cobrado)
    st.markdown(
        f'<div style="background:#f6f5ff; border:1px solid #e1ddf7; border-radius:10px; '
        f'padding:10px 14px; margin:6px 0; font-size:0.85rem; color:#45443e;">'
        f'Efectivo cobrado en la puerta: <b>${fmt_money(efectivo_cobrado)}</b><br>'
        f'Base entregada: <b>${fmt_money(base_monto)}</b><br>'
        f'<span style="color:#4b43b0;">Debe traer de vuelta (base + efectivo): '
        f'<b>${fmt_money(esperado_vuelta)}</b></span></div>',
        unsafe_allow_html=True)
    entregado = int(st.number_input(
        "💵 Efectivo que entrega el repartidor ($)", min_value=0, value=int(esperado_vuelta),
        step=1000, format="%d", key=f"liq_{base_id}",
        help="Cuenta TODO el efectivo que te da el repartidor (un solo fajo); el sistema "
             "reparte cuánto es cobro y cuánto son vueltas.") or 0)
    dif = entregado - esperado_vuelta
    if dif == 0:
        st.success("✅ Cuadra: el efectivo entregado coincide con lo esperado.")
    elif dif > 0:
        st.info(f"Sobra ${fmt_money(dif)} sobre lo esperado (¿cobró de más o quedó propina?).")
    else:
        st.error(f"⚠️ Faltan ${fmt_money(-dif)} respecto a lo esperado. Verifica antes de cerrar.")

    monto = _vueltas_devueltas(entregado, efectivo_cobrado, base_monto)
    st.markdown(
        f'<div style="font-size:0.8rem; color:#6b6b64; margin-top:2px;">De ese efectivo: '
        f'${fmt_money(efectivo_cobrado)} ya están registrados como cobro · '
        f'<b style="color:#45443e;">${fmt_money(monto)} son vueltas que vuelven al cajón</b>'
        f'</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        # Doble candado: deshabilitado en UI + rechazado en servidor (registrar_retorno_base).
        if st.button("🟢 Cerrar base", key=f"ret_confirm_{base_id}", type="primary",
                     use_container_width=True, disabled=saldo > 0):
            ok, msg = registrar_retorno_base(base_id, monto, entregado=entregado,
                                             esperado=esperado_vuelta)
            if ok:
                flash(f"Base cerrada · vueltas devueltas ${fmt_money(monto)}", "🟢")
                st.rerun()
            else:
                st.error(msg)
    with c2:
        if st.button("Volver", key=f"ret_volver_{base_id}", use_container_width=True):
            st.rerun()


# D — Cobro masivo en efectivo de los pedidos de UNA base de repartidor. La mayoría de
# domicilios se pagan en efectivo exacto en la puerta; obligar a un modal de cobro por cada
# pedido (con su propio método/comprobante) es fricción innecesaria cuando el repartidor ya
# trae todo en efectivo. Los pedidos por transferencia se cobran aparte (botón individual).
@st.dialog("💵 Cobrar todo en efectivo")
def _dialog_cobro_masivo(bid: int, nombre: str, ids: list, saldo_total: int):
    st.markdown(f"Repartidor **{html.escape(nombre)}** · {len(ids)} pedido(s) en efectivo · "
               f"total **${fmt_money(saldo_total)}**.")
    st.caption("Si algún pedido se pagó por transferencia, cóbralo individual antes de usar "
              "este botón (aquí solo se registran pagos en efectivo).")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("💵 Confirmar cobro", key=f"basecobro_masivo_confirm_{bid}",
                     type="primary", use_container_width=True):
            aplicado = pedidos.registrar_pago(ids, saldo_total, "efectivo")
            if aplicado <= 0:
                flash("⚠️ Estos pedidos ya estaban cobrados (otra caja o doble toque); "
                     "no se registró de nuevo.", "⚠️")
                st.rerun()
            audit.registrar("cobrar", "pedido", None, {
                "ids": ids, "monto": int(aplicado), "metodo": "efectivo",
                "via": "base_repartidor_bulk", "base_id": int(bid),
            })
            # Abre el cajón (sin imprimir papel: es un cobro de domicilio, no una cuenta de
            # mesa) para que el cajero guarde el efectivo entregado.
            enqueue_recibo(ids, f"Base · {nombre}", saldo_total, aplicado, "efectivo",
                           imprimir=False)
            flash(f"Cobrado en efectivo · ${fmt_money(aplicado)} · {len(ids)} pedido(s)", "💵")
            st.rerun()
    with c2:
        if st.button("Volver", key=f"basecobro_masivo_volver_{bid}", use_container_width=True):
            st.rerun()


# ── Sección de flujo de caja (dentro del turno abierto) ─────────────────────────
_MOV_LABEL = {
    "gasto":           ("🧾 Gasto", "#dc2626"),
    "reingreso_gasto": ("↩️ Devolución", "#16a34a"),
    "base_repartidor": ("🛵 Base repartidor", "#dc2626"),
    "retorno_base":    ("🟢 Vueltas devueltas", "#16a34a"),
}


def _seccion_flujo_caja(cierre: dict):
    cid = int(cierre["id"])
    st.markdown(titulo_seccion('💸 Flujo de caja · gastos y repartidores'),
                unsafe_allow_html=True)

    b1, b2 = st.columns(2)
    with b1:
        if st.button("🧾 Registrar gasto de caja", key="btn_open_gasto",
                     use_container_width=True):
            _dialog_gasto(cid)
    with b2:
        if st.button("🛵 Base de repartidor", key="btn_open_base",
                     use_container_width=True):
            # A.5 — higiene de estado: una base anterior no debe dejar nombre/monto/selección
            # pegados en el diálogo (las etiquetas del multiselect cambian con cada apertura,
            # ya que embeben montos dinámicos; sin este pop, un valor viejo en session_state
            # podría no calzar con las opciones nuevas).
            for _k in ("base_nombre", "base_monto", "base_pedidos", "_base_prev_sel"):
                st.session_state.pop(_k, None)
            _dialog_base(cid)

    # Gastos abiertos (esperan la devolución del cambio).
    gastos = movimientos_abiertos(cid, "gasto")
    for g in gastos:
        gid = int(g["id"])
        motivo = str(g["motivo"] or "")
        quien = f" · {html.escape(str(g['actor_nombre']))}" if g["actor_nombre"] else ""
        col_a, col_b = st.columns([3, 1])
        with col_a:
            st.markdown(
                f'<div class="order-card" style="border-left:4px solid #dc2626;">'
                f'<div style="font-size:0.85rem; color:#45443e;">🧾 Gasto abierto · '
                f'<b>${fmt_money(g["monto"])}</b>{quien}</div>'
                f'<div style="font-size:0.78rem; color:#a3a39b;">{html.escape(motivo)}</div></div>',
                unsafe_allow_html=True,
            )
        with col_b:
            if st.button("↩️ Devolver", key=f"btn_reing_{gid}", use_container_width=True):
                _dialog_reingreso(gid, int(g["monto"]), motivo)

    # Bases de repartidor abiertas: el repartidor está en ruta con su base de cambio y sus
    # pedidos. Al volver se cobran los pedidos (💵 Cobrar → libro 'pagos') y se cierra
    # la base devolviendo las vueltas sobrantes.
    bases = movimientos_abiertos(cid, "base_repartidor")
    for b in bases:
        bid = int(b["id"])
        nombre = str(b["actor_nombre"] or "Repartidor")
        base_monto = int(b["monto"])
        # Fuente de verdad: los pedidos ENLAZADOS por base_id (H1), no el JSON pedidos_ref.
        ordenes = pedidos_de_base(bid)
        if ordenes is None:
            st.warning(f"⚠️ No se pudieron leer los pedidos de la base de {html.escape(nombre)} "
                       "(conexión). El cierre queda bloqueado por seguridad; reintenta.")
            continue
        n_ped      = len(ordenes)
        total_ped  = sum(o["total"] for o in ordenes)
        cobrado_ped = sum(o["cobrado"] for o in ordenes)
        pend_saldo = sum(o["saldo"] for o in ordenes)
        color_pend = "#16a34a" if pend_saldo == 0 else "#dc2626"

        # Tarjeta de conciliación: base entregada + cuánto se ha cobrado de sus pedidos y
        # cuánto falta. Un vistazo dice si el repartidor ya cuadró.
        st.markdown(
            f'<div class="order-card" style="border-left:4px solid #6c5ce0; margin-bottom:6px;">'
            f'<div style="font-size:0.9rem; color:#45443e;">🛵 Repartidor en ruta · '
            f'<b>{html.escape(nombre)}</b></div>'
            f'<div style="font-size:0.8rem; color:#6b6b64; margin-top:2px;">'
            f'Base ${fmt_money(base_monto)} · {n_ped} pedido(s) · '
            f'cobrado ${fmt_money(cobrado_ped)} de ${fmt_money(total_ped)} · '
            f'<b style="color:{color_pend};">por cobrar ${fmt_money(pend_saldo)}</b>'
            f'</div></div>',
            unsafe_allow_html=True,
        )

        # D — Cobro masivo: si hay más de un pedido pendiente EN EFECTIVO, un solo botón
        # cobra todos de un golpe (el caso típico: el repartidor trae todo en efectivo). Los
        # pedidos por transferencia se quedan con su botón individual.
        efectivo_ids = [int(o["id"]) for o in ordenes
                        if o["saldo"] > 0 and (o["metodo_pago"] or "efectivo") == "efectivo"]
        efectivo_saldo = sum(o["saldo"] for o in ordenes
                             if o["saldo"] > 0 and (o["metodo_pago"] or "efectivo") == "efectivo")
        if len(efectivo_ids) > 1:
            if st.button(f"💵 Cobrar todo en efectivo (${fmt_money(efectivo_saldo)})",
                         key=f"btn_basecobro_masivo_{bid}", use_container_width=True):
                _dialog_cobro_masivo(bid, nombre, efectivo_ids, efectivo_saldo)

        # E — Un botón de cobro por cada pedido del repartidor que aún tenga saldo, con el
        # detalle de vueltas que ya lleva impreso en la hoja de ruta (mismo criterio: solo
        # efectivo con paga_con conocido exige vueltas). El cobro usa el modal compartido
        # (efectivo/transferencia + comprobante), que registra en 'pagos' → entra a las
        # ventas en efectivo del arqueo (no por movimientos_caja).
        for o in ordenes:
            oid = int(o["id"])
            col_o, col_b = st.columns([3, 1])
            with col_o:
                if o["saldo"] <= 0:
                    detalle = "✓ cobrado"
                elif o["vueltas"] > 0:
                    detalle = (f'por cobrar ${fmt_money(o["saldo"])} · paga con '
                              f'${fmt_money(o["paga_con"])} → vueltas ${fmt_money(o["vueltas"])}')
                elif (o["metodo_pago"] or "efectivo") != "efectivo":
                    detalle = f'por cobrar ${fmt_money(o["saldo"])} · 📱 transferencia'
                else:
                    detalle = f'por cobrar ${fmt_money(o["saldo"])}'
                st.markdown(
                    f'<div style="font-size:0.8rem; color:#6b6b64; padding:4px 0;">'
                    f'#{oid} · {html.escape(str(o["nombre"]))} · {detalle}</div>',
                    unsafe_allow_html=True,
                )
            with col_b:
                if st.button("💵 Cobrar", key=f"btn_basecobro_{bid}_{oid}",
                             use_container_width=True, disabled=o["saldo"] <= 0):
                    pedidos.dialog_cobrar([oid], f"Pedido #{oid} · {nombre}",
                                          int(o["saldo"]), f"basecobro_{bid}_{oid}")

        # Cerrar la base queda BLOQUEADO mientras haya saldo por cobrar (defensa también en
        # el servidor: registrar_retorno_base lo rechaza). Así no se cierra una base con
        # cobros sin registrar → la caja no se descuadra.
        cerrar_bloqueado = pend_saldo > 0
        if st.button("🟢 Cerrar base · recibir vueltas", key=f"btn_ret_{bid}",
                     use_container_width=True, disabled=cerrar_bloqueado,
                     help=("Cobra primero los pedidos pendientes para poder cerrar."
                           if cerrar_bloqueado else
                           "Devuelve las vueltas sobrantes y concilia la base.")):
            _dialog_retorno(bid, base_monto, nombre, pend_saldo)
        if cerrar_bloqueado:
            st.caption(f"⚠️ Faltan ${fmt_money(pend_saldo)} por cobrar para cerrar esta base.")

    # Histórico compacto del turno + neto.
    _bloque_historial_movimientos(cid)


# ── Confirmación de cierre (doble chequeo del monto contado) ────────────────────
@st.dialog("🔒 Confirmar cierre de caja")
def _dialog_confirmar_cierre(cierre_id: int, efvo_esp: int, transf_esp: int,
                             efectivo_real: int, transferencia_real: int,
                             diferencia: int, total_esperado: int):
    """Reconfirma los montos CONTADOS antes de cerrar el turno. Muestra grandes el efectivo
    y las transferencias que el cajero está a punto de registrar para que los verifique
    contra el dinero físico (no revela lo esperado → respeta el conteo a ciegas). Solo al
    pulsar 'Sí, cerrar caja' se congela el arqueo (irreversible)."""
    st.markdown(
        '<div style="color:#45443e; font-size:0.9rem; margin-bottom:10px;">'
        'Revisa el dinero contado antes de cerrar el turno: verifica que coincida con lo '
        'que tienes físicamente. Esta acción no se puede deshacer.</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div style="display:flex; gap:10px; margin-bottom:4px;">'
        '<div style="flex:1; background:#f6f7f9; border:1px solid #e6e8ec; border-radius:10px; '
        'padding:10px 14px;">'
        '<div style="font-size:0.78rem; color:#6b6b64;">💵 Efectivo contado</div>'
        '<div style="font-family:\'DM Sans\',sans-serif; font-size:1.5rem; font-weight:800; '
        f'color:#26262b;">${fmt_money(efectivo_real)}</div></div>'
        '<div style="flex:1; background:#f6f7f9; border:1px solid #e6e8ec; border-radius:10px; '
        'padding:10px 14px;">'
        '<div style="font-size:0.78rem; color:#6b6b64;">💳 Transferencias</div>'
        '<div style="font-family:\'DM Sans\',sans-serif; font-size:1.5rem; font-weight:800; '
        f'color:#26262b;">${fmt_money(transferencia_real)}</div></div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.caption("Si algún monto no coincide, vuelve y corrígelo antes de cerrar.")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ Sí, cerrar caja", key="btn_confirmar_cierre_final",
                     type="primary", use_container_width=True):
            cerrar_caja(cierre_id, efvo_esp, transf_esp,
                        efectivo_real, transferencia_real, diferencia)
            if diferencia == 0:
                estado = "cuadrada"
            elif diferencia < 0:
                estado = f"faltante ${fmt_money(-diferencia)}"
            else:
                estado = f"sobrante ${fmt_money(diferencia)}"
            # Guarda el resultado para REVELARLO en el próximo render (clave del conteo a
            # ciegas: la caja recién aquí se entera de si cuadró o cuánto faltó/sobró).
            st.session_state["_cierre_resultado"] = {
                "diferencia": int(diferencia),
                "efectivo_real": int(efectivo_real),
                "total_esperado": int(total_esperado),
            }
            flash(f"Turno cerrado · caja {estado}", "💰")
            st.rerun()
    with c2:
        if st.button("← Volver a contar", key="btn_volver_cierre",
                     use_container_width=True):
            st.rerun()


def _bloque_historial_movimientos(cid: int):
    """Histórico compacto del turno (neto + expander con cada movimiento). Compartido por
    la sección de flujo de caja del admin y la caja simple del cajero."""
    movs = movimientos_del_turno(cid)
    if not movs:
        return
    neto = neto_movimientos(cid)
    signo = "+" if neto >= 0 else "−"
    color = "#16a34a" if neto >= 0 else "#dc2626"
    st.markdown(
        f'<div style="font-size:0.82rem; color:#45443e; margin-top:8px;">Efecto neto en el '
        f'cajón: <b style="color:{color};">{signo}${fmt_money(abs(neto))}</b></div>',
        unsafe_allow_html=True,
    )
    with st.expander(f"Ver movimientos del turno ({len(movs)})"):
        for mv in movs:
            etiqueta, c = _MOV_LABEL.get(mv["tipo"], (mv["tipo"], "#6b6b64"))
            signo_mv = "+" if mv["tipo"] in TIPOS_ENTRADA else "−"
            extra = f" · {html.escape(str(mv['motivo'] or mv['actor_nombre'] or ''))}".rstrip(" ·")
            st.markdown(
                f'<div style="display:flex; justify-content:space-between; font-size:0.8rem; '
                f'padding:4px 0; border-bottom:1px solid #f2f1ed;">'
                f'<span style="color:#45443e;">{etiqueta} · {_fechahora(mv["creado_at"])}'
                f'{"" if extra == " · " else extra}</span>'
                f'<span style="color:{c}; font-weight:700;">{signo_mv}${fmt_money(mv["monto"])}</span></div>',
                unsafe_allow_html=True,
            )


def _bloque_resultado_cierre():
    """Revelación del resultado del último cierre (conteo a ciegas): se muestra una sola
    vez, justo después de finalizar el turno. Compartida por el render de admin y el de
    la caja simple — misma regla, mismo texto en los dos."""
    _res = st.session_state.pop("_cierre_resultado", None)
    if not _res:
        return
    dif = int(_res["diferencia"])
    contado = int(_res["efectivo_real"])
    if dif == 0:
        _pill("#dcfce7", "#86efac", "#14532d",
              f"✅ Caja cuadrada. Contaste ${fmt_money(contado)} y coincide con lo esperado.")
    elif dif < 0:
        _pill("#fef3c7", "#fcd34d", "#92400e",
              f"⚠️ Faltante en caja: -${fmt_money(-dif)} (contaste ${fmt_money(contado)}).")
    else:
        _pill("#e9e7fb", "#bcb4f0", "#4b43b0",
              f"ℹ️ Sobrante en caja: +${fmt_money(dif)} (contaste ${fmt_money(contado)}).")
    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# CAJA SIMPLE · home del cajero (botones grandes)
# ══════════════════════════════════════════════════════════════════════════════
# Reutiliza los MISMOS diálogos y helpers de BD que usa el admin (_dialog_gasto,
# _dialog_base, _dialog_retorno, _dialog_cobro_masivo, _dialog_confirmar_cierre,
# pedidos.dialog_cobrar…): esta sección solo cambia la PRESENTACIÓN, nunca la lógica de
# negocio ni los candados de caja (conteo a ciegas, H1, H2, anti-skimming).
#
# Streamlit no permite abrir un @st.dialog dentro de otro en el mismo run. Los diálogos
# de la caja simple (Repartidores, Gastos, Cobrar) necesitan abrir un SEGUNDO diálogo
# desde dentro (p. ej. "Cobrar" de una mesa → pedidos.dialog_cobrar). Se resuelve con el
# mismo patrón "pedir/abrir" que ya usa monitor_mesas.py: se guarda la intención en
# session_state y se relanza; el diálogo real se abre en el run siguiente, DESDE FUERA
# del diálogo que lo pidió.
def _pedir_dialogo_simple(kind: str, **args) -> None:
    st.session_state["_caja_simple_dialog"] = {"kind": kind, **args}
    st.rerun()


def _abrir_dialogo_simple_pendiente() -> None:
    d = st.session_state.get("_caja_simple_dialog")
    if not d:
        return
    st.session_state["_caja_simple_dialog"] = None   # one-shot: Streamlit mantiene el modal abierto
    kind = d.get("kind")
    if kind == "cobrar":
        pedidos.dialog_cobrar(d["ids"], d["titulo"], d["saldo"], d["uid"])
    elif kind == "base":
        _dialog_base(d["cierre_id"])
    elif kind == "retorno":
        _dialog_retorno(d["bid"], d["base_monto"], d.get("nombre", ""), d.get("pend_saldo", 0))
    elif kind == "cobro_masivo":
        _dialog_cobro_masivo(d["bid"], d["nombre"], d["ids"], d["saldo_total"])
    elif kind == "gasto":
        _dialog_gasto(d["cierre_id"])
    elif kind == "reingreso":
        _dialog_reingreso(d["gasto_id"], d["gasto_monto"], d.get("motivo", ""))
    elif kind == "confirmar_cierre":
        _dialog_confirmar_cierre(d["cierre_id"], d["efvo_esp"], d["transf_esp"],
                                 d["efectivo_real"], d["transferencia_real"],
                                 d["diferencia"], d["total_esperado"])


def _on_abrir_caja_dismiss() -> None:
    """El usuario cerró el modal con la ✕/Esc. Como el despacho es PERSISTENTE (bandera
    '_dlg_abrir_caja_open': render() reabre el diálogo en cada run mientras siga en
    True), hay que limpiar el estado aquí o el diálogo se reabriría de inmediato en el
    siguiente rerun — quedaría atrapado sin poder cerrarse. Streamlit re-ejecuta solo
    tras el callback, así que NO llamamos st.rerun() (mismo patrón que
    monitor_mesas._on_edit_dismiss)."""
    st.session_state["_dlg_abrir_caja_open"] = False
    st.session_state.pop("_dlg_abrir_caja_id", None)


@st.dialog("🟢 Abrir caja", on_dismiss=_on_abrir_caja_dismiss)
def _dialog_abrir_caja():
    """Apertura de turno EN DOS PASOS, compartida por admin y caja simple: (1) define la
    base de efectivo, (2) si el cierre anterior dejó meseros bloqueados (cerrar_caja los
    bloquea en bloque), los activa aquí mismo — así el cajero no tiene que ir aparte a
    Administración cada mañana solo para que los meseros puedan volver a entrar.

    Se mantiene abierto entre reruns con el mismo mecanismo de flag persistente en
    session_state que usa el resto de la caja simple: mientras '_dlg_abrir_caja_open' siga
    en True, render() vuelve a llamar esta función en cada run (ver abajo)."""
    if not st.session_state.get("_dlg_abrir_caja_id"):
        monto_apertura = int(st.number_input(
            "Monto de Apertura (Base en Efectivo)", min_value=0, value=0, step=1000,
            format="%d", key="dlg_monto_apertura",
            help="Efectivo con el que arranca la caja al inicio del turno.",
        ) or 0)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🟢 Abrir Caja / Iniciar Turno", key="dlg_btn_abrir_caja",
                         type="primary", use_container_width=True):
                if abrir_caja(monto_apertura):
                    flash(f"Caja abierta · base ${fmt_money(monto_apertura)}", "🟢")
                    st.session_state["_dlg_abrir_caja_id"] = True
                    st.rerun()
                else:
                    st.error("Ya hay un turno abierto.")
        with c2:
            if st.button("Cancelar", key="dlg_btn_abrir_caja_cancelar",
                         use_container_width=True):
                st.session_state["_dlg_abrir_caja_open"] = False
                st.rerun()
        return

    # Paso 2: el turno ya abrió. Meseros que quedaron bloqueados por el cierre anterior.
    bloqueados = [e for e in empleados.listar_empleados(incluir_inactivos=False)
                  if e["rol"] == "mesero" and e["bloqueado"]]
    if not bloqueados:
        st.session_state["_dlg_abrir_caja_open"] = False
        st.session_state.pop("_dlg_abrir_caja_id", None)
        st.rerun()
        return

    st.success("Turno iniciado.")
    st.caption("Estos meseros quedaron bloqueados por el cierre anterior. Actívalos para "
               "que su PIN vuelva a servir.")
    for e in bloqueados:
        eid = int(e["id"])
        col_a, col_b = st.columns([3, 1])
        with col_a:
            st.markdown(
                f'<div style="font-size:0.9rem; color:#45443e; padding:6px 0;">'
                f'🧑‍🍳 {html.escape(str(e["nombre"]))}</div>', unsafe_allow_html=True)
        with col_b:
            if st.button("Activar", key=f"dlg_activar_mesero_{eid}",
                         use_container_width=True):
                if empleados.reactivar_acceso(eid):
                    # Mismo nombre de evento que usaba la vista Meseros (Reactivar),
                    # para no fragmentar el libro mayor en dos taxonomías distintas.
                    audit.registrar("acceso_reactivado", "empleado", eid,
                                    {"nombre": e["nombre"], "via": "apertura_caja"})
                    flash(f"{e['nombre']} activado", "🟢")
                st.rerun()
    if len(bloqueados) > 1:
        if st.button("Activar todos", key="dlg_activar_todos", use_container_width=True):
            for e in bloqueados:
                eid = int(e["id"])
                if empleados.reactivar_acceso(eid):
                    audit.registrar("acceso_reactivado", "empleado", eid,
                                    {"nombre": e["nombre"], "via": "apertura_caja"})
            flash("Todos los meseros activados", "🟢")
            st.rerun()
    if st.button("Listo, empezar turno", key="dlg_abrir_caja_listo", type="primary",
                 use_container_width=True):
        st.session_state["_dlg_abrir_caja_open"] = False
        st.session_state.pop("_dlg_abrir_caja_id", None)
        st.rerun()


@st.dialog("🗄️ Abrir cajón")
def _dialog_abrir_cajon_simple(cierre_id: int):
    st.caption("Abre el cajón sin que haya una venta detrás (p. ej. cambiar un billete). "
               "Queda registrado en la auditoría.")
    motivo = st.text_input("Motivo (opcional)", key="dlg_cajon_motivo",
                           placeholder="Ej: cambio de billete de $50.000")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗄️ Abrir cajón", key="dlg_cajon_confirmar", type="primary",
                     use_container_width=True):
            enqueue_abrir_cajon(motivo)
            audit.registrar("cajon_abierto", "caja", int(cierre_id),
                            {"motivo": (motivo or "").strip() or None})
            flash("Cajón abierto", "🗄️")
            st.rerun()
    with c2:
        if st.button("Cancelar", key="dlg_cajon_cancelar", use_container_width=True):
            st.rerun()


@st.dialog("👥 Equipo del turno")
def _dialog_equipo_turno():
    """Salida (bloquear acceso) / Reactivar de un MESERO a media jornada — no solo al
    abrir caja (_dialog_abrir_caja ya cubre los que quedaron bloqueados del cierre
    anterior, pero no a alguien que llega tarde o hay que sacar a medio turno). Antes
    esto vivía en la vista Meseros, que ahora es solo-admin (P3); esto reabre SOLO ese
    gesto puntual para el cajero, sin la gestión de perfiles/PIN (crear, regenerar PIN,
    dar de baja siguen siendo manage_empleados → solo admin, ver views/meseros.py)."""
    lista = [e for e in empleados.listar_empleados(incluir_inactivos=False)
             if e["rol"] == "mesero"]
    if not lista:
        st.caption("No hay meseros con perfil activo.")
    for e in lista:
        eid = int(e["id"])
        nombre = str(e["nombre"])
        col_a, col_b = st.columns([3, 1.2])
        with col_a:
            estado = "🔒 Bloqueado" if e["bloqueado"] else "🟢 Activo"
            st.markdown(
                f'<div style="font-size:0.9rem; color:#45443e; padding:6px 0;">'
                f'{html.escape(nombre)} · {estado}</div>', unsafe_allow_html=True)
        with col_b:
            if e["bloqueado"]:
                if st.button("▶ Reactivar", key=f"equipo_reactivar_{eid}",
                             use_container_width=True):
                    if empleados.reactivar_acceso(eid):
                        audit.registrar("acceso_reactivado", "empleado", eid,
                                        {"nombre": nombre, "via": "equipo_turno"})
                        flash(f"Acceso reactivado · {nombre}", "▶")
                    st.rerun()
            else:
                if st.button("⏹ Salida", key=f"equipo_salida_{eid}",
                             use_container_width=True,
                             help="Cerrar turno: bloquea el PIN y cierra su sesión ahora"):
                    if empleados.bloquear_acceso(eid):
                        audit.registrar("acceso_bloqueado", "empleado", eid,
                                        {"nombre": nombre, "via": "equipo_turno"})
                        flash(f"Turno cerrado · {nombre} (su acceso queda bloqueado)", "🔒")
                    st.rerun()
    if st.button("Cerrar", key="equipo_turno_cerrar", use_container_width=True):
        st.rerun()


@st.dialog("💵 Cobrar")
def _dialog_cobrar_mesas():
    cuentas = _cuentas_por_cobrar_hoy()
    if cuentas is None:
        st.error("⚠️ No se pudieron leer las cuentas pendientes (conexión). Reintenta en "
                 "unos segundos.")
        if st.button("Volver", key="simple_cobrarmesas_err", use_container_width=True):
            st.rerun()
        return
    if cuentas:
        st.caption("Elige la cuenta a cobrar.")
        for c in cuentas:
            col_a, col_b = st.columns([3, 1])
            with col_a:
                icono = "🍽️" if c["tipo"] == "mesa" else "🛍️"
                st.markdown(
                    f'<div style="font-size:0.9rem; color:#45443e; padding:6px 0;">{icono} '
                    f'<b>{html.escape(str(c["nombre"]))}</b> · restan ${fmt_money(c["saldo"])}'
                    f'</div>', unsafe_allow_html=True)
            with col_b:
                key = f"simple_cobrarmesa_{c['tipo']}_{'_'.join(map(str, c['ids']))}"
                if st.button("Cobrar", key=key, use_container_width=True):
                    _pedir_dialogo_simple("cobrar", ids=c["ids"], titulo=c["nombre"],
                                          saldo=c["saldo"], uid=key)
    else:
        st.info("No hay cuentas pendientes de cobro por ahora.")

    # Cobradas hoy: sin esto, reimprimir un recibo era casi inalcanzable (esa opción
    # solo vive dentro del checkout de una cuenta, y una cuenta ya saldada no aparece en
    # ningún otro listado). 'Reimprimir' es una acción directa (no abre otro diálogo),
    # así que se llama sin pasar por el despacho pedir/abrir.
    cobradas = _cuentas_cobradas_hoy()
    if cobradas:
        st.divider()
        with st.expander(f"Cobradas hoy ({len(cobradas)})"):
            for c in cobradas:
                col_a, col_b = st.columns([3, 1])
                with col_a:
                    icono = "🍽️" if c["tipo"] == "mesa" else "🛍️"
                    st.markdown(
                        f'<div style="font-size:0.85rem; color:#6b6b64; padding:6px 0;">'
                        f'{icono} {html.escape(str(c["nombre"]))} · '
                        f'${fmt_money(c["total"])}</div>', unsafe_allow_html=True)
                with col_b:
                    key = f"simple_reimprimir_{c['tipo']}_{'_'.join(map(str, c['ids']))}"
                    if st.button("🖨️", key=key, use_container_width=True,
                                help="Reimprimir recibo"):
                        pedidos.reimprimir_recibo(c["ids"], c["nombre"])
                        flash(f"Recibo reimpreso · {c['nombre']}", "🖨️")
                        st.rerun()

    if st.button("Cerrar", key="simple_cobrarmesas_cerrar", use_container_width=True):
        st.rerun()


@st.dialog("🛵 Repartidores")
def _dialog_repartidores_simple(cierre_id: int):
    cid = int(cierre_id)
    bases = movimientos_abiertos(cid, "base_repartidor")
    if not bases:
        st.caption("No hay repartidores en ruta.")
    for b in bases:
        bid = int(b["id"])
        nombre = str(b["actor_nombre"] or "Repartidor")
        base_monto = int(b["monto"])
        ordenes = pedidos_de_base(bid)
        if ordenes is None:
            st.warning(f"⚠️ No se pudieron leer los pedidos de la base de {html.escape(nombre)} "
                       "(conexión). Reintenta.")
            continue
        n_ped = len(ordenes)
        total_ped = sum(o["total"] for o in ordenes)
        cobrado_ped = sum(o["cobrado"] for o in ordenes)
        pend_saldo = sum(o["saldo"] for o in ordenes)
        color_pend = "#16a34a" if pend_saldo == 0 else "#dc2626"
        st.markdown(
            f'<div class="order-card" style="border-left:4px solid #6c5ce0; margin-bottom:6px;">'
            f'<div style="font-size:0.9rem; color:#45443e;">🛵 <b>{html.escape(nombre)}</b></div>'
            f'<div style="font-size:0.8rem; color:#6b6b64; margin-top:2px;">'
            f'Base ${fmt_money(base_monto)} · {n_ped} pedido(s) · '
            f'cobrado ${fmt_money(cobrado_ped)} de ${fmt_money(total_ped)} · '
            f'<b style="color:{color_pend};">por cobrar ${fmt_money(pend_saldo)}</b></div></div>',
            unsafe_allow_html=True,
        )
        efectivo_ids = [int(o["id"]) for o in ordenes
                        if o["saldo"] > 0 and (o["metodo_pago"] or "efectivo") == "efectivo"]
        efectivo_saldo = sum(o["saldo"] for o in ordenes
                             if o["saldo"] > 0 and (o["metodo_pago"] or "efectivo") == "efectivo")
        if len(efectivo_ids) > 1:
            if st.button(f"💵 Cobrar todo en efectivo (${fmt_money(efectivo_saldo)})",
                         key=f"simple_basecobro_masivo_{bid}", use_container_width=True):
                _pedir_dialogo_simple("cobro_masivo", bid=bid, nombre=nombre,
                                      ids=efectivo_ids, saldo_total=efectivo_saldo)
        for o in ordenes:
            oid = int(o["id"])
            if o["saldo"] <= 0:
                continue
            col_o, col_b = st.columns([3, 1])
            with col_o:
                detalle = f'por cobrar ${fmt_money(o["saldo"])}'
                if o["vueltas"] > 0:
                    detalle += f' · vueltas ${fmt_money(o["vueltas"])}'
                st.markdown(
                    f'<div style="font-size:0.8rem; color:#6b6b64; padding:4px 0;">'
                    f'#{oid} · {html.escape(str(o["nombre"]))} · {detalle}</div>',
                    unsafe_allow_html=True,
                )
            with col_b:
                if st.button("💵 Cobrar", key=f"simple_basecobro_{bid}_{oid}",
                             use_container_width=True):
                    _pedir_dialogo_simple("cobrar", ids=[oid],
                                          titulo=f"Pedido #{oid} · {nombre}",
                                          saldo=int(o["saldo"]), uid=f"simplebase_{bid}_{oid}")
        cerrar_bloqueado = pend_saldo > 0
        if st.button("🟢 Cerrar base · recibir vueltas", key=f"simple_ret_{bid}",
                     use_container_width=True, disabled=cerrar_bloqueado,
                     help=("Cobra primero los pedidos pendientes para poder cerrar."
                           if cerrar_bloqueado else
                           "Devuelve las vueltas sobrantes y concilia la base.")):
            _pedir_dialogo_simple("retorno", bid=bid, base_monto=base_monto, nombre=nombre,
                                  pend_saldo=pend_saldo)
        if cerrar_bloqueado:
            st.caption(f"⚠️ Faltan ${fmt_money(pend_saldo)} por cobrar para cerrar esta base.")
        st.divider()
    if st.button("🛵 Nueva base de repartidor", key="simple_btn_nueva_base", type="primary",
                 use_container_width=True):
        # Higiene de estado (ver _seccion_flujo_caja): una base anterior no debe dejar
        # nombre/monto/selección pegados en el diálogo.
        for _k in ("base_nombre", "base_monto", "base_pedidos", "_base_prev_sel"):
            st.session_state.pop(_k, None)
        _pedir_dialogo_simple("base", cierre_id=cid)
    if st.button("Cerrar", key="simple_repartidores_cerrar", use_container_width=True):
        st.rerun()


@st.dialog("🧾 Gasto de caja")
def _dialog_gastos_simple(cierre_id: int):
    cid = int(cierre_id)
    gastos = movimientos_abiertos(cid, "gasto")
    if gastos:
        st.caption("Gastos abiertos · esperan la devolución del cambio.")
        for g in gastos:
            gid = int(g["id"])
            motivo = str(g["motivo"] or "")
            quien = f" · {html.escape(str(g['actor_nombre']))}" if g["actor_nombre"] else ""
            col_a, col_b = st.columns([3, 1])
            with col_a:
                st.markdown(
                    f'<div style="font-size:0.85rem; color:#45443e; padding:6px 0;">'
                    f'${fmt_money(g["monto"])}{quien}<br>'
                    f'<span style="font-size:0.78rem; color:#a3a39b;">{html.escape(motivo)}'
                    f'</span></div>',
                    unsafe_allow_html=True,
                )
            with col_b:
                if st.button("↩️ Devolver", key=f"simple_reing_{gid}", use_container_width=True):
                    _pedir_dialogo_simple("reingreso", gasto_id=gid,
                                          gasto_monto=int(g["monto"]), motivo=motivo)
        st.divider()
    if st.button("🧾 Nuevo gasto", key="simple_btn_nuevo_gasto", type="primary",
                 use_container_width=True):
        _pedir_dialogo_simple("gasto", cierre_id=cid)
    if st.button("Cerrar", key="simple_gastos_cerrar", use_container_width=True):
        st.rerun()


@st.dialog("🔒 Cerrar turno")
def _dialog_cerrar_turno_simple(cierre: dict):
    # La caja SIEMPRE cuenta a ciegas (esta vista solo renderiza para quien no ve
    # ingresos): sin métricas de venta ni diferencia en vivo, igual que el formulario
    # de cierre del admin cuando ver_esperado=False.
    cid = int(cierre["id"])
    monto_apertura = int(cierre["monto_apertura"])
    esp = ingresos_esperados(cierre["fecha_apertura"])
    efvo_esp = esp["efectivo"]
    transf_esp = esp["transferencia"]
    neto_mov = neto_movimientos(cid)
    total_esperado = monto_apertura + efvo_esp + neto_mov

    st.caption("🙈 Conteo a ciegas: cuenta el efectivo y las transferencias y finaliza el "
               "turno. Al cerrar verás si la caja cuadró o si falta/sobra.")

    efectivo_real = int(st.number_input(
        "Efectivo Físico Contado ($)", min_value=0, value=0, step=1000,
        format="%d", key=f"simple_efectivo_real_{cid}",
        help="Dinero en efectivo realmente contado en la caja.",
    ) or 0)
    transferencia_real = int(st.number_input(
        "Transferencias Verificadas en Banco ($)", min_value=0, value=0, step=1000,
        format="%d", key=f"simple_transferencia_real_{cid}",
        help="Transferencias confirmadas en la cuenta bancaria.",
    ) or 0)
    diferencia = efectivo_real - total_esperado

    if st.button("Finalizar Turno y Cerrar Caja", key="simple_btn_finalizar_cierre",
                 type="primary", use_container_width=True):
        _pedir_dialogo_simple("confirmar_cierre", cierre_id=cid, efvo_esp=efvo_esp,
                              transf_esp=transf_esp, efectivo_real=efectivo_real,
                              transferencia_real=transferencia_real, diferencia=diferencia,
                              total_esperado=total_esperado)
    if st.button("Volver", key="simple_btn_cerrar_volver", use_container_width=True):
        st.rerun()


def _tarjeta_simple(col, icono: str, titulo: str, subtitulo: str, key: str, on_click):
    """Una tarjeta grande y clicable de la home del cajero: icono + título + subtítulo de
    estado (p. ej. 'N sin devolución') y un botón 'Abrir' de ancho completo."""
    with col:
        with st.container(border=True):
            st.markdown(
                f'<div style="text-align:center; padding:4px 0;">'
                f'<div style="font-size:1.8rem; line-height:1;">{icono}</div>'
                f'<div style="font-family:\'DM Sans\',sans-serif; font-weight:800; '
                f'font-size:1.05rem; color:#26262b; margin-top:6px;">{titulo}</div>'
                f'<div style="font-size:0.8rem; color:#6b6b64; margin-top:2px;">{subtitulo}</div>'
                f'</div>', unsafe_allow_html=True,
            )
            if st.button("Abrir", key=key, use_container_width=True):
                on_click()


def _render_caja_simple():
    """Home del cajero: sin pestañas, sin inventario/importar, sin métricas de venta — 4
    acciones grandes que cubren el turno completo (cobrar, repartidores, gastos, cerrar).
    Ver cabecera de esta sección: reutiliza los diálogos y helpers del admin tal cual."""
    _abrir_dialogo_simple_pendiente()

    st.markdown(
        "<style>"
        ".st-key-simple_tile_cobrar button, .st-key-simple_tile_repartidores button, "
        ".st-key-simple_tile_gasto button, .st-key-simple_tile_cerrar button {"
        "height:52px !important; font-weight:700 !important; font-size:1rem !important;}"
        "</style>",
        unsafe_allow_html=True,
    )
    st.markdown(titulo_seccion('💰 Caja'), unsafe_allow_html=True)
    st.markdown(
        f'<div style="margin:-4px 0 12px 0;">{badge_agente_html()}</div>',
        unsafe_allow_html=True,
    )
    _bloque_resultado_cierre()

    cierre = cierre_activo()

    if not cierre:
        st.markdown(
            '<div class="order-card" style="text-align:center; padding:1.6rem 1rem;">'
            '<div style="font-size:2rem; line-height:1;">🔒</div>'
            '<div style="font-family:\'DM Sans\',sans-serif; font-size:1.3rem; '
            'font-weight:800; color:#26262b; margin-top:6px;">La caja se encuentra cerrada.</div>'
            '<div style="color:#6b6b64; font-size:0.9rem; margin-top:4px;">Define la base en '
            'efectivo e inicia un nuevo turno para comenzar a operar.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🟢 Abrir Caja / Iniciar Turno", key="simple_btn_abrir_caja",
                     type="primary", use_container_width=True):
            st.session_state["_dlg_abrir_caja_open"] = True
            st.rerun()
        return

    cid = int(cierre["id"])
    st.markdown(
        f'<div class="order-card" style="border-left:4px solid #16a34a; margin-bottom:1rem;">'
        f'<div class="order-id">Turno abierto · desde las {_hora(cierre["fecha_apertura"])}</div>'
        f'<div style="color:#6b6b64; font-size:0.85rem; margin-top:2px;">'
        f'Base ${fmt_money(int(cierre["monto_apertura"]))} · 🙈 Conteo a ciegas</div></div>',
        unsafe_allow_html=True,
    )

    n_cuentas = _cuentas_por_cobrar_hoy()
    if n_cuentas is None:
        sub_cobrar = "—"
    elif n_cuentas:
        sub_cobrar = f"{len(n_cuentas)} cuenta(s) con saldo"
    else:
        sub_cobrar = "Sin pendientes"
    bases_abiertas = movimientos_abiertos(cid, "base_repartidor")
    gastos_abiertos = movimientos_abiertos(cid, "gasto")

    r1c1, r1c2 = st.columns(2)
    _tarjeta_simple(r1c1, "💵", "Cobrar mesa", sub_cobrar, "simple_tile_cobrar",
                    _dialog_cobrar_mesas)
    _tarjeta_simple(r1c2, "🛵", "Repartidores",
                    f"{len(bases_abiertas)} en ruta" if bases_abiertas else "Ninguno en ruta",
                    "simple_tile_repartidores", lambda: _dialog_repartidores_simple(cid))

    r2c1, r2c2 = st.columns(2)
    _tarjeta_simple(r2c1, "🧾", "Gasto de caja",
                    f"{len(gastos_abiertos)} sin devolución" if gastos_abiertos else
                    "Ninguno abierto",
                    "simple_tile_gasto", lambda: _dialog_gastos_simple(cid))
    _tarjeta_simple(r2c2, "🔒", "Cerrar turno", "Contar y finalizar",
                    "simple_tile_cerrar", lambda: _dialog_cerrar_turno_simple(cierre))

    st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
    s1, s2 = st.columns(2)
    with s1:
        if st.button("🗄️ Abrir cajón (cambio)", key="simple_btn_abrir_cajon",
                     use_container_width=True):
            _dialog_abrir_cajon_simple(cid)
    with s2:
        if st.button("👥 Equipo del turno", key="simple_btn_equipo_turno",
                     use_container_width=True,
                     help="Da salida o reactiva a un mesero a media jornada"):
            _dialog_equipo_turno()

    st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)
    _bloque_historial_movimientos(cid)


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: CAJA · CIERRE DE TURNO
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Guard de capacidad (defensa en profundidad): el router ya oculta Caja al mesero,
    # pero validamos también aquí por si la vista quedó forzada por query param.
    if not auth.can("manage_caja"):
        st.error("🔒 Acceso denegado")
        st.stop()
    # Apertura de turno: diálogo compartido por admin y caja simple (ver _dialog_abrir_caja).
    # Se re-invoca en CADA run mientras el flag siga activo — así Streamlit mantiene el
    # mismo diálogo abierto a través de sus dos pasos (base → activar meseros).
    if st.session_state.get("_dlg_abrir_caja_open"):
        _dialog_abrir_caja()
    # El admin (ve_revenue) usa el entorno completo: cierre + Inventario e Importar
    # (movidos desde 🍔 Menú, reutilizados tal cual sin duplicar su lógica). El cajero usa
    # una home simplificada de botones grandes (_render_caja_simple): sin pestañas, sin
    # inventario/importar — esas son tareas de configuración, no del turno del día a día.
    if auth.can("see_revenue"):
        tab_cierre, tab_inv, tab_imp = st.tabs(
            ["💰 Cierre de caja", "📦 Inventario", "📥 Importar"])
        with tab_cierre:
            _render_cierre()
        with tab_inv:
            menu._render_inventario()
        with tab_imp:
            menu._render_importar()
    else:
        _render_caja_simple()


def _render_cierre():
    # Botón slate (formal) para finalizar el turno, vía estilo por st-key.
    st.markdown(
        "<style>"
        ".st-key-btn_finalizar_cierre button{background:#2e2d29 !important;"
        "border-color:#2e2d29 !important;color:#fff !important;}"
        ".st-key-btn_finalizar_cierre button:hover{background:#26262b !important;"
        "border-color:#26262b !important;color:#fff !important;}"
        "</style>",
        unsafe_allow_html=True,
    )
    st.markdown(titulo_seccion('💰 Caja · cierre de turno'),
                unsafe_allow_html=True)

    # Salud del Agente de Impresión Local (heartbeat): el cajero ve de un vistazo si los
    # recibos van a salir y cuántos hay en cola, sin enterarse por un cliente sin ticket.
    st.markdown(
        f'<div style="margin:-4px 0 12px 0;">{badge_agente_html()}</div>',
        unsafe_allow_html=True,
    )

    # Resultado del último cierre (REVELACIÓN del conteo a ciegas): tras finalizar el turno se
    # muestra aquí, prominente, si la caja cuadró o cuánto faltó/sobró. Se consume una sola vez
    # (la caja contó a ciegas, así que este es el momento en que se entera del resultado).
    _bloque_resultado_cierre()

    cierre = cierre_activo()

    # ── ESTADO A: caja cerrada (sin turno activo) ───────────────────────────────
    if not cierre:
        st.markdown(
            '<div class="order-card" style="text-align:center; padding:1.6rem 1rem;">'
            '<div style="font-size:2rem; line-height:1;">🔒</div>'
            '<div style="font-family:\'DM Sans\',sans-serif; font-size:1.3rem; '
            'font-weight:800; color:#26262b; margin-top:6px;">La caja se encuentra cerrada.</div>'
            '<div style="color:#6b6b64; font-size:0.9rem; margin-top:4px;">Define la base en '
            'efectivo e inicia un nuevo turno para comenzar a operar.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.markdown("<br>", unsafe_allow_html=True)

        if st.button("🟢 Abrir Caja / Iniciar Turno", key="btn_abrir_caja",
                     type="primary", use_container_width=True):
            st.session_state["_dlg_abrir_caja_open"] = True
            st.rerun()

    # ── ESTADO B: caja abierta (turno activo) ───────────────────────────────────
    else:
        monto_apertura = int(cierre["monto_apertura"])
        esp = ingresos_esperados(cierre["fecha_apertura"])
        efvo_esp = esp["efectivo"]
        transf_esp = esp["transferencia"]
        # Efectivo esperado = base + ventas en efectivo + efecto neto del flujo de caja
        # (gastos/devoluciones y bases de repartidor/depósitos). Así el arqueo concilia
        # automáticamente con el dinero que salió y entró del cajón fuera de las ventas.
        neto_mov = neto_movimientos(int(cierre["id"]))
        total_esperado = monto_apertura + efvo_esp + neto_mov

        # ¿Se revela lo ESPERADO? El conteo a ciegas (control anti-descuadre) oculta a la CAJA
        # cuánto debería haber: el empleado cuenta, cierra y SOLO ENTONCES ve si cuadró. El admin
        # (see_revenue → único con visibilidad de ventas) sí ve lo esperado y la diferencia en vivo.
        ver_esperado = auth.can("see_revenue")

        # Banner de turno abierto. El lado derecho muestra lo esperado solo al admin; a la caja
        # le explica el conteo a ciegas en su lugar.
        if ver_esperado:
            lado_html = (
                '<div style="text-align:right;">'
                '<div class="metric-label">Esperado en caja</div>'
                f'<div class="order-total" style="font-size:1.4rem;">${fmt_money(total_esperado)}</div>'
                '</div>'
            )
        else:
            lado_html = (
                '<div style="text-align:right; max-width:240px;">'
                '<div class="metric-label">🙈 Conteo a ciegas</div>'
                '<div style="font-size:0.8rem; color:#6b6b64; line-height:1.3;">Cuenta el efectivo y '
                'cierra; al finalizar el sistema te dirá si cuadra.</div>'
                '</div>'
            )
        st.markdown(f"""
        <div class="order-card" style="border-left:4px solid #16a34a; margin-bottom:1rem;">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
              <div class="order-id">Turno abierto</div>
              <div style="font-family:'DM Sans',sans-serif; font-size:1.2rem; font-weight:600; color:#26262b;">
                Desde las {_hora(cierre["fecha_apertura"])}</div>
            </div>
            {lado_html}
          </div>
        </div>
        """, unsafe_allow_html=True)

        # Métricas en vivo del turno. Las ventas ESPERADAS solo para quien ve ingresos (admin);
        # a la caja se le muestra únicamente la base (no compromete el conteo a ciegas).
        if ver_esperado:
            m1, m2, m3 = st.columns(3)
            _metric(m1, f"${fmt_money(monto_apertura)}", "Base de Apertura")
            _metric(m2, f"${fmt_money(efvo_esp)}", "💵 Ventas Efectivo Esperadas", "metric-green")
            _metric(m3, f"${fmt_money(transf_esp)}", "💳 Ventas Transferencia Esperadas", "metric-blue")
        else:
            _metric(st.columns(1)[0], f"${fmt_money(monto_apertura)}", "Base de Apertura")

        cols = st.columns([3, 1])
        with cols[1]:
            if st.button("🔄 Actualizar", key="btn_caja_refrescar", use_container_width=True):
                st.rerun()

        st.divider()

        # ── Flujo de caja: gastos y bases de repartidor (afecta lo esperado) ─────
        _seccion_flujo_caja(cierre)

        st.divider()

        # ── Formulario de cierre ────────────────────────────────────────────────
        with st.container(border=True):
            st.markdown(
                '<div style="font-family:\'DM Sans\',sans-serif; font-size:1.05rem; '
                'font-weight:800; color:#26262b; margin-bottom:2px;">🔒 Formulario de Cierre de Caja</div>'
                '<div style="color:#6b6b64; font-size:0.85rem; margin-bottom:10px;">Cuenta el dinero '
                'físico y verifica las transferencias en el banco antes de finalizar el turno.</div>',
                unsafe_allow_html=True,
            )

            # value por defecto: el admin (ve ingresos) arranca con lo esperado para verificar
            # rápido; a la CAJA se le deja en 0 — conteo a ciegas: no debe ver el esperado, ni
            # siquiera prellenado en el campo.
            efvo_default = max(0, total_esperado) if ver_esperado else 0
            transf_default = max(0, transf_esp) if ver_esperado else 0

            f1, f2 = st.columns(2)
            with f1:
                # value se acota a >=0: 'total_esperado' puede salir NEGATIVO si los gastos /
                # bases superan la base + ventas (el efectivo no puede ser negativo), y un
                # value < min_value rompía el number_input. La diferencia sigue usando el
                # total_esperado real (negativo) más abajo.
                efectivo_real = int(st.number_input(
                    "Efectivo Físico Contado ($)", min_value=0, value=efvo_default,
                    step=1000, format="%d", key=f"efectivo_real_{cierre['id']}",
                    help="Dinero en efectivo realmente contado en la caja.",
                ) or 0)
            with f2:
                transferencia_real = int(st.number_input(
                    "Transferencias Verificadas en Banco ($)", min_value=0, value=transf_default,
                    step=1000, format="%d", key=f"transferencia_real_{cierre['id']}",
                    help="Transferencias confirmadas en la cuenta bancaria.",
                ) or 0)

            # Diferencia de caja (solo efectivo). Se calcula siempre, pero el conteo a ciegas
            # solo la REVELA tras cerrar: la caja no la ve en vivo (no podría "cuadrar a mano"),
            # el admin sí.
            diferencia = efectivo_real - total_esperado
            st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)
            if ver_esperado:
                if diferencia == 0:
                    _pill("#dcfce7", "#86efac", "#14532d", "✅ Caja cuadrada perfectamente.")
                elif diferencia < 0:
                    _pill("#fef3c7", "#fcd34d", "#92400e",
                          f"⚠️ Faltante en caja: -${fmt_money(-diferencia)}")
                else:
                    _pill("#e9e7fb", "#bcb4f0", "#4b43b0",
                          f"ℹ️ Sobrante en caja: +${fmt_money(diferencia)}")

                # Contraste de transferencias (contexto; no afecta la caja física).
                dif_transf = transferencia_real - transf_esp
                if dif_transf == 0:
                    transf_txt = "coinciden con lo esperado"
                elif dif_transf < 0:
                    transf_txt = f"faltan ${fmt_money(-dif_transf)} por verificar"
                else:
                    transf_txt = f"hay ${fmt_money(dif_transf)} de más sobre lo esperado"
                st.caption(f"💳 Transferencias verificadas vs. esperadas: {transf_txt}.")
            else:
                st.caption("🙈 Conteo a ciegas: cuenta el efectivo y las transferencias y finaliza "
                           "el turno. Al cerrar verás si la caja cuadró o si falta/sobra.")

            st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)
            # Cierre en DOS pasos: el botón abre una confirmación que muestra grandes los
            # montos contados para que el cajero los verifique contra el dinero físico antes
            # de congelar el arqueo (evita cerrar con un monto mal tecleado).
            if st.button("Finalizar Turno y Cerrar Caja", key="btn_finalizar_cierre",
                         use_container_width=True):
                _dialog_confirmar_cierre(int(cierre["id"]), efvo_esp, transf_esp,
                                         efectivo_real, transferencia_real, diferencia,
                                         total_esperado)

    # ── Historial de turnos cerrados ────────────────────────────────────────────
    recientes = cierres_recientes()
    if recientes:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="section-title">Turnos cerrados</div>', unsafe_allow_html=True)
        for t in recientes:
            dif = int(t["diferencia"] or 0)
            if dif == 0:
                color, etiqueta = "#16a34a", "cuadrada"
            elif dif > 0:
                color, etiqueta = "#6c5ce0", f"sobrante ${fmt_money(dif)}"
            else:
                color, etiqueta = "#dc2626", f"faltante ${fmt_money(-dif)}"
            efectivo_real = int(t["efectivo_real"]) if t["efectivo_real"] is not None else 0
            transf_real = int(t["transferencia_real"]) if t["transferencia_real"] is not None else 0
            st.markdown(f"""
            <div class="order-card" style="border-left:4px solid {color};">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <div style="font-size:0.85rem; color:#45443e;">
                  {_fechahora(t["fecha_apertura"])} → {_fechahora(t["fecha_cierre"])}
                  <span style="color:#a3a39b;"> · base ${fmt_money(t["monto_apertura"])} · efectivo ${fmt_money(efectivo_real)} · transf. ${fmt_money(transf_real)}</span>
                </div>
                <div style="color:{color}; font-weight:700; font-size:0.85rem;">{html.escape(etiqueta)}</div>
              </div>
            </div>
            """, unsafe_allow_html=True)
