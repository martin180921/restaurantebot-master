"""Vista de Pedidos: tablero de estados, alertas de audio y tickets de cocina."""
import streamlit as st
import streamlit.components.v1
from sqlalchemy import text, bindparam
import pandas as pd
import json
import html
from datetime import datetime

import auth
import audit
import empleados
from db import (engine, fmt_money, fecha_corta, flash, drain_toasts,
                saldo_pedido, cobrado_pedido, _es_pagado, _a_entero,
                aplicar_inventario)
from utils.print_jobs import enqueue_recibo, enqueue_comanda
from utils.items import (formatear_items_html, lineas_por_categoria,
                         parse_items, etiqueta_item)

# ── Constantes ─────────────────────────────────────────────────────────────────
# Flujo de cocina simplificado: pendiente (recibido) → listo → entregado. Ya no hay
# paso manual de "Iniciar preparación"; el pedido aparece en cocina al crearse y la
# comanda se imprime en ese momento (ver utils.print_jobs.enqueue_comanda llamado por
# nuevo_pedido al confirmar). 'en preparacion' se MANTIENE reconocido para que los
# pedidos heredados que quedaron en ese estado sigan mostrándose y avanzando a 'listo';
# ningún pedido nuevo entra ya en él.
ESTADOS = ["pendiente", "en preparacion", "listo", "entregado"]
ESTADO_SIGUIENTE = {
    "pendiente":      "listo",          # antes pasaba por "en preparacion"
    "en preparacion": "listo",          # solo heredados: los empuja directo a listo
    "listo":          "entregado",
    "entregado":      None
}
ESTADO_LABEL_BTN = {
    "pendiente":      "✓ Marcar listo",  # antes "▶ Iniciar preparación"
    "en preparacion": "✓ Marcar listo",  # heredados
    "listo":          "✓ Entregar",
    "entregado":      None
}


# F1: el esquema (columna motivo_cancelacion) lo garantiza db._ensure_schema()
# al importar db.py, así que aquí ya está disponible.
ESTADOS_ACTIVOS = ["pendiente", "en preparacion", "listo"]


# ── Toasts no bloqueantes (Fase 1) ──────────────────────────────────────────────
# flash()/drain_toasts() viven en db.py (compartidos por todas las vistas). Se
# reexportan aquí para no romper las llamadas existentes (pedidos.flash /
# pedidos.drain_toasts) desde panel.py y monitor_mesas.py.


# ── DB: pedidos ────────────────────────────────────────────────────────────────
# P-CAJA: cacheada con TTL corto. Antes era una lectura SIN caché que corría en
# CADA rerun de los tres fragmentos en vivo (tablero, monitor, web) — uno por mesa
# cada 30 s — y, peor, en CADA pulsación de los number_input del modal de cobro (un
# rerun por tecla → un SELECT por tecla), lo que congelaba la caja bajo movimiento.
# Con la caché: los reruns concurrentes comparten un único SELECT y las teclas del
# modal no tocan la BD (el cálculo de cambio es Python puro e instantáneo). El TTL
# de 8 s mantiene el refresco en vivo "suficientemente fresco"; las ESCRITURAS
# llaman refrescar_pedidos() para que las acciones se reflejen al instante.
@st.cache_data(ttl=8)
def cargar_pedidos():
    # P-SCALE: lectura ACOTADA en el servidor (antes 'SELECT * FROM pedidos' sin filtro
    # cargaba la tabla entera en cada cache-miss → crecía sin límite y congelaba la caja
    # a los meses). El tablero en vivo solo necesita: lo que está en cocina, lo que tiene
    # saldo pendiente (de cualquier día) y todo lo de HOY (para stats/ventas del día). El
    # histórico completo lo sirven las vistas de Resumen/Cancelaciones con su propia query.
    # Apoyado en idx_pedidos_estado / idx_pedidos_fecha / idx_pedidos_no_pagado.
    with engine.connect() as conn:
        resultado = conn.execute(text("""
            SELECT * FROM pedidos
            WHERE estado IN ('pendiente', 'en preparacion', 'listo')
               OR (pagado = FALSE AND total > COALESCE(total_pagado, 0)
                   AND estado <> 'cancelado')
               OR fecha::date = CURRENT_DATE
            ORDER BY fecha DESC
        """))
        return pd.DataFrame(resultado.fetchall(), columns=resultado.keys())

def refrescar_pedidos():
    """Invalida la caché del tablero tras una escritura (cobro, avance, cancelación,
    cambio de mesa) para que el cambio se vea en el siguiente run sin esperar el TTL."""
    cargar_pedidos.clear()

def avanzar_estado(pedido_id: int, estado_actual: str):
    siguiente = ESTADO_SIGUIENTE.get(estado_actual)
    if not siguiente:
        return
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pedidos SET estado = :estado WHERE id = :id"),
            {"estado": siguiente, "id": pedido_id}
        )
    # La comanda ya NO se dispara aquí: con el flujo simplificado se imprime al CREAR el
    # pedido (nuevo_pedido.crear_pedido_manual), no al avanzar de estado.
    refrescar_pedidos()
    flash(f"Pedido #{pedido_id} → {siguiente}", "✅")
    st.rerun()

def revertir_estado(pedido_id: int, estado_actual: str):
    idx = ESTADOS.index(estado_actual)
    if idx == 0:
        return
    anterior = ESTADOS[idx - 1]
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pedidos SET estado = :estado WHERE id = :id"),
            {"estado": anterior, "id": pedido_id}
        )
    refrescar_pedidos()
    flash(f"Pedido #{pedido_id} → {anterior}", "↩️")
    st.rerun()

# ── Regla anti-skimming (FASE 1) ────────────────────────────────────────────────
# Una vez que la cuenta TOCÓ CAJA no se puede cancelar: cobro iniciado (se abrió el
# checkout), abono parcial (total_pagado>0) o pago completo (pagado). Es el control
# contra el robo hormiga clásico: cobrar al cliente, anular la venta y quedarse el
# efectivo. La regla se aplica en la UI (botón bloqueado) Y aquí en el servidor (un id
# forzado por otra ruta no la salta).
def puede_cancelar(row) -> bool:
    """True si el pedido está 'limpio' (sin interacción de caja) y por tanto cancelable.
    Acepta filas pandas o dict; defensivo ante columnas faltantes pre-migración."""
    if _es_pagado(row.get("pagado", False)):
        return False
    if _a_entero(row.get("total_pagado", 0)) > 0:
        return False
    if _es_pagado(row.get("cobro_iniciado", False)):   # bool seguro ante None/NaN
        return False
    return True


def marcar_cobro_iniciado(ids) -> None:
    """Sella cobro_iniciado=TRUE en cuanto la cuenta entra al checkout (abrir el modal de
    cobro o aplicar un descuento). A partir de aquí la cancelación queda bloqueada. Solo
    audita la primera vez que un id cambia de estado (no en cada rerun del modal)."""
    ids = [int(i) for i in ids]
    if not ids:
        return
    upd = text(
        "UPDATE pedidos SET cobro_iniciado = TRUE "
        "WHERE id IN :ids AND COALESCE(cobro_iniciado, FALSE) = FALSE AND estado <> 'cancelado'"
    ).bindparams(bindparam("ids", expanding=True))
    try:
        with engine.begin() as conn:
            n = conn.execute(upd, {"ids": ids}).rowcount or 0
        if n:
            refrescar_pedidos()
            audit.registrar("checkout_iniciado", "pedido", ids[0], {"ids": ids})
    except Exception:
        pass


def cancelar_pedido(pedido_id: int, motivo: str = ""):
    # cancelled_at = NOW() sella la hora de la cancelación para el historial del
    # administrador (Caja → Cancelaciones, agrupado por día). Guard anti-skimming en
    # transacción (FOR UPDATE): si entre la lectura de la UI y este punto la cuenta tocó
    # caja, se rechaza. La cancelación se anota en el libro mayor con el actor.
    pedido_id = int(pedido_id)
    bloqueado = False
    total_canc = 0
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT total, items, COALESCE(total_pagado, 0) AS tp, pagado, "
            "COALESCE(cobro_iniciado, FALSE) AS ci, estado "
            "FROM pedidos WHERE id = :id FOR UPDATE"
        ), {"id": pedido_id}).mappings().first()
        if not row or row["estado"] == "cancelado":
            return
        if row["pagado"] or int(row["tp"]) > 0 or row["ci"]:
            bloqueado = True
        else:
            total_canc = int(row["total"])
            conn.execute(
                text("UPDATE pedidos SET estado = 'cancelado', motivo_cancelacion = :m, "
                     "cancelled_at = NOW() WHERE id = :id"),
                {"m": (motivo or "").strip() or None, "id": pedido_id}
            )
            # Reversa de inventario SOLO si se cancela ANTES de 'listo': lo que aún no se
            # cocinó vuelve al stock. Un pedido ya 'listo'/'entregado' NO se reintegra (la
            # comida ya se preparó). Mismo txn que la cancelación → atómico.
            if row["estado"] in ("pendiente", "en preparacion"):
                aplicar_inventario(conn, row["items"], +1)
    if bloqueado:
        # Defensa en profundidad: la UI ya oculta el botón, pero si se llega aquí avisamos.
        flash("🔒 No se puede cancelar: la cuenta ya entró a caja.", "🔒")
        st.rerun()
        return
    refrescar_pedidos()
    audit.registrar("cancelar_pedido", "pedido", pedido_id,
                    {"motivo": (motivo or "").strip() or None, "total": total_canc})
    flash(f"Pedido #{pedido_id} cancelado", "🗑️")
    st.rerun()


# ── Descuentos y cortesías autorizados (FASE 1) ─────────────────────────────────
# Rebaja del saldo de un pedido, desbloqueada solo con PIN de administrador (lo valida
# el modal con empleados.admin_pin_valido) y con justificación obligatoria. Recalcula el
# total NETO (el saldo baja en la rebaja) conservando la rebaja acumulada en
# descuento_valor (gross = total + descuento_valor) y deja rastro en el libro mayor.
TIPOS_DESCUENTO = {"monto", "porcentaje", "cortesia"}


def aplicar_descuento(pedido_id: int, tipo: str, valor, motivo: str, autoriza: str) -> tuple:
    """Aplica una rebaja autorizada y devuelve (ok, mensaje).
    tipo: 'monto' (COP fijos) | 'porcentaje' (% del total actual) | 'cortesia' (comp del
    saldo restante → saldo 0). Marca cobro_iniciado (la cuenta entró a caja). Atómico."""
    pedido_id = int(pedido_id)
    tipo = (tipo or "").lower()
    if tipo not in TIPOS_DESCUENTO:
        return False, "Tipo de descuento inválido."
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT total, COALESCE(total_pagado, 0) AS tp, COALESCE(descuento_valor, 0) AS dv, "
            "pagado, estado FROM pedidos WHERE id = :id FOR UPDATE"
        ), {"id": pedido_id}).mappings().first()
        if not row or row["estado"] == "cancelado":
            return False, "Pedido no disponible."
        if row["pagado"]:
            return False, "El pedido ya está pagado por completo."
        total, tp, dv = int(row["total"]), int(row["tp"]), int(row["dv"])
        saldo = max(0, total - tp)
        if saldo <= 0:
            return False, "No hay saldo por descontar."
        if tipo == "monto":
            rebaja = min(int(valor or 0), saldo)
        elif tipo == "porcentaje":
            pct = max(0, min(100, int(valor or 0)))
            rebaja = min(saldo, round(total * pct / 100))
        else:  # cortesia → comp del saldo restante
            rebaja = saldo
        rebaja = int(rebaja)
        if rebaja <= 0:
            return False, "El descuento calculado es $0."
        nuevo_total = total - rebaja
        saldado = nuevo_total <= tp          # si el neto ya está cubierto, queda pagado
        conn.execute(text(
            "UPDATE pedidos SET total = :t, descuento_valor = :dv, tipo_descuento = :tp_, "
            "motivo_descuento = :mo, descuento_autoriza = :au, cobro_iniciado = TRUE, "
            "pagado = :pg WHERE id = :id"
        ), {"t": nuevo_total, "dv": dv + rebaja, "tp_": tipo,
            "mo": (motivo or "").strip()[:500] or None, "au": (autoriza or "")[:120],
            "pg": bool(saldado), "id": pedido_id})
    refrescar_pedidos()
    audit.registrar("cortesia" if tipo == "cortesia" else "descuento", "pedido", pedido_id,
                    {"tipo": tipo, "valor": int(valor or 0), "monto": rebaja,
                     "total_antes": total, "total_despues": nuevo_total,
                     "motivo": (motivo or "").strip() or None, "autoriza": autoriza})
    etiqueta = "Cortesía" if tipo == "cortesia" else "Descuento"
    return True, f"{etiqueta} aplicado · −${fmt_money(rebaja)}"


# ── DB: cobro (pagos completos y parciales) ─────────────────────────────────────
# 'pagado' es una dimensión aparte del estado de cocina: cobrar NO toca el flujo
# pendiente→…→entregado, solo registra el pago. Una mesa se libera cuando todos sus
# pedidos están pagados (o cancelados), no cuando se entregan.
def _distribuir_abono(pendientes, monto):
    """Reparte 'monto' entre 'pendientes' (del más antiguo al más nuevo) sin pasarse
    del saldo de cada pedido. Función PURA (sin BD) para poder testearla.

    'pendientes' = lista de dicts {id, total, total_pagado}. Devuelve la lista de
    actualizaciones [{id, total_pagado, pagado, aplicado}] solo para los pedidos
    tocados; 'aplicado' = lo abonado a ese pedido ahora (una fila del libro 'pagos').
    """
    restante = max(0, int(monto))
    updates = []
    for p in pendientes:
        if restante <= 0:
            break
        total = int(p["total"])
        ya = int(p.get("total_pagado") or 0)
        saldo = max(0, total - ya)
        if saldo <= 0:
            continue
        aplicar = min(saldo, restante)
        nuevo = ya + aplicar
        updates.append({"id": int(p["id"]), "total_pagado": nuevo,
                        "pagado": nuevo >= total, "aplicado": aplicar})
        restante -= aplicar
    return updates


# Submétodos válidos de transferencia (billeteras locales). NULL en efectivo.
SUBMETODOS_TRANSF = {"nequi", "daviplata", "breb"}


def registrar_pago(ids, monto, metodo="efectivo", submetodo=None, comprobante=None):
    """Registra un abono de 'monto' (en 'metodo') contra los pedidos 'ids' (uno o
    varios), repartiéndolo del más antiguo al más nuevo. Cada pedido cuyo saldo quede
    en 0 se marca pagado=TRUE; el resto acumula en total_pagado y sigue pagado=FALSE.
    Cada abono aplicado se anota además en el libro 'pagos' (método + submétodo +
    comprobante + hora real de pago). Lee y escribe en la MISMA transacción
    (FOR UPDATE) sobre el saldo real.

    submetodo/comprobante solo aplican a transferencias; en efectivo se guardan NULL.
    """
    ids = [int(i) for i in ids]
    monto = int(round(float(monto or 0)))
    metodo = metodo if metodo in ("efectivo", "transferencia") else "efectivo"
    if metodo == "transferencia":
        sub = (str(submetodo or "").strip().lower() or None)
        sub = sub if sub in SUBMETODOS_TRANSF else None
        comp = (str(comprobante or "").strip()[:60] or None)
    else:
        sub, comp = None, None   # el efectivo no lleva submétodo ni comprobante
    if not ids or monto <= 0:
        return
    sel = text("""
        SELECT id, total, COALESCE(total_pagado, 0) AS total_pagado
        FROM pedidos
        WHERE id IN :ids AND pagado = FALSE AND estado <> 'cancelado'
        ORDER BY fecha, id
        FOR UPDATE
    """).bindparams(bindparam("ids", expanding=True))
    upd = text("UPDATE pedidos SET total_pagado = :total_pagado, pagado = :pagado WHERE id = :id")
    ins = text("INSERT INTO pagos (pedido_id, monto, metodo, submetodo, comprobante) "
               "VALUES (:pedido_id, :monto, :metodo, :submetodo, :comprobante)")
    with engine.begin() as conn:
        pendientes = [dict(r) for r in conn.execute(sel, {"ids": ids}).mappings().all()]
        for u in _distribuir_abono(pendientes, monto):
            conn.execute(upd, {"total_pagado": u["total_pagado"], "pagado": u["pagado"], "id": u["id"]})
            conn.execute(ins, {"pedido_id": u["id"], "monto": u["aplicado"], "metodo": metodo,
                               "submetodo": sub, "comprobante": comp})
    # El cobro cambió saldos/ocupación → invalida el tablero para reflejarlo al instante.
    refrescar_pedidos()


# ── DB: cobro POR PLATO (pagar unidades de líneas concretas) ────────────────────
# 'total_pagado' sigue siendo la autoridad del saldo; pago_lineas es un libro auxiliar
# que recuerda QUÉ unidades de cada línea (índice en pedidos.items) ya se cobraron, para
# pintar el checklist de lo que falta. Invariante en modo por-plato:
# total_pagado == Σ(cantidad_pagada × precio). Solo aplica a UN pedido a la vez.
def _precio_unitario(item) -> int:
    try:
        return int(round(float(item.get("precio") or 0)))
    except (TypeError, ValueError):
        return 0


def lineas_pagables(pedido_id: int):
    """[{idx, nombre, precio, cantidad, pagada, restante}] de un pedido: cada línea de
    items con cuántas unidades quedan por cobrar (cantidad − pagada). Tolerante a fallos."""
    pedido_id = int(pedido_id)
    try:
        with engine.connect() as conn:
            raw = conn.execute(text("SELECT items FROM pedidos WHERE id = :id"),
                               {"id": pedido_id}).scalar_one_or_none()
            pagadas = {int(r["linea_idx"]): int(r["cantidad_pagada"]) for r in conn.execute(
                text("SELECT linea_idx, cantidad_pagada FROM pago_lineas WHERE pedido_id = :id"),
                {"id": pedido_id}).mappings().all()}
    except Exception:
        return []
    out = []
    for idx, it in enumerate(parse_items(raw)):
        cantidad = int(it.get("cantidad", 1) or 1)
        pagada = min(cantidad, pagadas.get(idx, 0))
        out.append({"idx": idx, "nombre": etiqueta_item(it), "precio": _precio_unitario(it),
                    "cantidad": cantidad, "pagada": pagada, "restante": max(0, cantidad - pagada)})
    return out


def valor_lineas_pagadas(pedido_id: int) -> int:
    """Σ(cantidad_pagada × precio) de un pedido. Si total_pagado supera este valor, hubo
    un abono por MONTO no atribuido a platos → el modo por-plato se oculta (evita
    descuadrar los dos libros)."""
    return sum(l["pagada"] * l["precio"] for l in lineas_pagables(pedido_id))


def registrar_pago_items(pedido_id, seleccion, metodo="efectivo", submetodo=None, comprobante=None):
    """Cobra unidades concretas de un pedido. 'seleccion' = {linea_idx: cantidad}. Cobra
    el subtotal de lo elegido (acotado al saldo real), avanza total_pagado/pagado, anota
    el abono en 'pagos' y suma las unidades en pago_lineas. Todo en UNA transacción."""
    pedido_id = int(pedido_id)
    seleccion = {int(k): int(v) for k, v in (seleccion or {}).items() if int(v) > 0}
    metodo = metodo if metodo in ("efectivo", "transferencia") else "efectivo"
    if metodo == "transferencia":
        sub = (str(submetodo or "").strip().lower() or None)
        sub = sub if sub in SUBMETODOS_TRANSF else None
        comp = (str(comprobante or "").strip()[:60] or None)
    else:
        sub, comp = None, None
    if not seleccion:
        return
    ins = text("INSERT INTO pagos (pedido_id, monto, metodo, submetodo, comprobante) "
               "VALUES (:pedido_id, :monto, :metodo, :submetodo, :comprobante)")
    ups = text("""
        INSERT INTO pago_lineas (pedido_id, linea_idx, cantidad_pagada)
        VALUES (:pid, :idx, :cant)
        ON CONFLICT (pedido_id, linea_idx)
        DO UPDATE SET cantidad_pagada = pago_lineas.cantidad_pagada + EXCLUDED.cantidad_pagada
    """)
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT items, total, COALESCE(total_pagado, 0) AS total_pagado, pagado "
            "FROM pedidos WHERE id = :id AND estado <> 'cancelado' FOR UPDATE"
        ), {"id": pedido_id}).mappings().first()
        if not row or row["pagado"]:
            return
        items = parse_items(row["items"])
        # Unidades ya pagadas por línea (lock para no cobrar dos veces en concurrencia).
        ya = {int(r["linea_idx"]): int(r["cantidad_pagada"]) for r in conn.execute(
            text("SELECT linea_idx, cantidad_pagada FROM pago_lineas WHERE pedido_id = :id "
                 "FOR UPDATE"), {"id": pedido_id}).mappings().all()}
        # Subtotal de lo elegido, acotando cada línea a lo realmente pendiente.
        subtotal, aplicar = 0, {}
        for idx, cant in seleccion.items():
            if idx < 0 or idx >= len(items):
                continue
            disp = max(0, int(items[idx].get("cantidad", 1) or 1) - ya.get(idx, 0))
            usar = min(cant, disp)
            if usar > 0:
                aplicar[idx] = usar
                subtotal += usar * _precio_unitario(items[idx])
        saldo = max(0, int(row["total"]) - int(row["total_pagado"]))
        cobro = min(subtotal, saldo)
        if cobro <= 0 or not aplicar:
            return
        nuevo_pagado = int(row["total_pagado"]) + cobro
        conn.execute(text("UPDATE pedidos SET total_pagado = :tp, pagado = :pg WHERE id = :id"),
                     {"tp": nuevo_pagado, "pg": nuevo_pagado >= int(row["total"]), "id": pedido_id})
        conn.execute(ins, {"pedido_id": pedido_id, "monto": cobro, "metodo": metodo,
                           "submetodo": sub, "comprobante": comp})
        for idx, usar in aplicar.items():
            conn.execute(ups, {"pid": pedido_id, "idx": idx, "cant": usar})
    refrescar_pedidos()


# ── DB: cambio de mesa (transferir cuenta a otra mesa) ──────────────────────────
# Mueve TODOS los pedidos activos de una mesa a otra reescribiendo su FK mesa_id en
# UNA sola transacción (atómico: o se mueven todos, o ninguno). La ocupación de las
# mesas es DERIVADA (una mesa está ocupada si tiene pedidos activos), así que al
# reasignar la FK la mesa origen queda libre y la destino ocupada sin tocar ninguna
# bandera. Acota a pedidos con saldo y sin cancelar para no arrastrar cuentas ya
# cerradas; mover por 'ids' explícitos cubre también los pedidos heredados cuya
# mesa_id era NULL (se les fija la mesa destino).
def mover_mesa(ids, mesa_destino: int) -> int:
    """Reasigna los pedidos 'ids' a 'mesa_destino'. Devuelve cuántos se movieron.
    Solo toca pedidos no pagados y no cancelados (cuenta viva). Atómico."""
    ids = [int(i) for i in ids]
    mesa_destino = int(mesa_destino)
    if not ids:
        return 0
    upd = text("""
        UPDATE pedidos SET mesa_id = :destino
        WHERE id IN :ids AND pagado = FALSE AND estado <> 'cancelado'
    """).bindparams(bindparam("ids", expanding=True))
    with engine.begin() as conn:
        res = conn.execute(upd, {"destino": mesa_destino, "ids": ids})
        movidos = res.rowcount or 0
    refrescar_pedidos()
    return movidos


# ── Helpers visuales ───────────────────────────────────────────────────────────
def badge_html(estado: str) -> str:
    cls = {
        "pendiente":      "badge-pendiente",
        "en preparacion": "badge-preparacion",
        "listo":          "badge-listo",
        "entregado":      "badge-entregado",
        "cancelado":      "badge-cancelado",
    }.get(estado, "badge-pendiente")
    label = {
        "pendiente":      "● Pendiente",
        "en preparacion": "◎ En preparación",
        "listo":          "✓ Listo",
        "entregado":      "✓ Entregado",
        "cancelado":      "✕ Cancelado",
    }.get(estado, estado)
    return f'<span class="badge {cls}">{label}</span>'

def formatear_items(items_raw) -> str:
    # Modelo por secciones (utils.items): resume cada item en una línea, con el
    # desglose del Plato del Día entre paréntesis, y agrega nota si la trae. Sigue
    # parseando el TEXT-JSON de la BD (C3) y escapando los nombres (C2). Retro-
    # compatible: los items legados sin 'tipo' se muestran como 'Nx Nombre'.
    return formatear_items_html(items_raw)

def formatear_fecha(fecha) -> str:
    if pd.isna(fecha):
        return "—"
    try:
        return fecha_corta(pd.to_datetime(fecha))  # U4: portable, en español
    except Exception:
        return str(fecha)


# ── Tiempo de espera (U2) ───────────────────────────────────────────────────────
def minutos_espera(fecha):
    """Minutos desde 'fecha', o None si no se puede calcular."""
    if pd.isna(fecha):
        return None
    try:
        dt = pd.to_datetime(fecha).to_pydatetime()
        return max(0, int((datetime.now() - dt).total_seconds() // 60))
    except Exception:
        return None

def urgencia(mins, estado):
    """Color de antigüedad para pedidos ACTIVOS: verde <5 min, ámbar 5-10, rojo
    >10. Devuelve un hex o None (sin acento para entregados/cancelados)."""
    if estado not in ("pendiente", "en preparacion") or mins is None:
        return None
    if mins >= 10:
        return "#dc2626"  # rojo: urgente
    if mins >= 5:
        return "#d97706"  # ámbar: atención
    return "#16a34a"      # verde: reciente


# ── Origen del pedido (U6) ──────────────────────────────────────────────────────
def icono_cliente(row, mesa_nombres=None):
    """(emoji, etiqueta) según el origen: 🪑 mesa (en local) o 📱 teléfono."""
    mid = row.get("mesa_id")
    cliente = str(row.get("numero_cliente", "—") or "—")
    if mid is not None and not pd.isna(mid):
        nombre = (mesa_nombres or {}).get(int(mid))
        return ("🪑", nombre or cliente)
    if cliente.lower().startswith("mesa"):
        return ("🪑", cliente)
    return ("📱", cliente)


# ── Ticket de cocina ───────────────────────────────────────────────────────────
def generar_ticket_html(pid, cliente, items_raw, total_p, fecha, estado):
    """Genera el HTML del ticket termico para imprimir."""
    # Modelo por secciones (utils.items): agrupa por categoría con sus componentes
    # indentados debajo. Retro-compatible: los items legados (sin 'tipo') caen en
    # 'A LA CARTA' como líneas simples. C2: todo se escapa antes de inyectarse.
    secciones = lineas_por_categoria(items_raw)
    if secciones:
        lineas_items = ""
        for cat_label, items in secciones:
            lineas_items += f'<div class="cat">[{html.escape(str(cat_label))}]</div>'
            for it in items:
                lineas_items += (f'<div class="it"><b>{int(it["cantidad"])}x</b> '
                                 f'{html.escape(str(it["nombre"]))}</div>')
                for et, val in it["componentes"]:
                    lineas_items += (f'<div class="cmp">* {html.escape(str(et))}: '
                                     f'{html.escape(str(val))}</div>')
    else:
        lineas_items = '<div class="it">—</div>'

    # C2: cliente y estado se escapan antes de inyectarse en el ticket.
    cliente    = html.escape(str(cliente))
    fecha_str  = html.escape(str(fecha)) if fecha else fecha_corta(datetime.now())
    estado_str = html.escape(str(estado).upper())
    total_fmt  = fmt_money(total_p)                                 # C6

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{font-family:'Courier New',monospace;font-size:13px;color:#000;background:#fff;width:280px;padding:12px;}}
  .header{{text-align:center;margin-bottom:8px;}}
  .restaurant{{font-size:16px;font-weight:bold;letter-spacing:1px;}}
  .sub{{font-size:11px;color:#333;margin-top:2px;}}
  .divider{{border-top:1px dashed #000;margin:8px 0;}}
  .label{{font-size:10px;text-transform:uppercase;color:#555;}}
  .value{{font-size:13px;font-weight:bold;}}
  .cat{{font-size:10px;font-weight:bold;letter-spacing:1px;margin:8px 0 2px 0;border-bottom:1px solid #000;}}
  .it{{font-size:13px;margin:2px 0;}}
  .it b{{font-weight:bold;}}
  .cmp{{font-size:11px;color:#222;padding-left:16px;}}
  .total-row{{font-size:16px;font-weight:bold;text-align:right;margin-top:8px;}}
  .footer{{text-align:center;margin-top:10px;font-size:11px;color:#444;}}
  .estado-badge{{display:inline-block;border:1px solid #000;padding:1px 8px;font-size:10px;margin-top:4px;}}
  @media print{{body{{width:100%;}} @page{{margin:4mm;size:80mm auto;}}}}
</style></head><body>
  <div class="header">
    <div class="restaurant">RESTAURANTE</div>
    <div class="sub">Control de Cocina</div>
  </div>
  <div class="divider"></div>
  <div class="label">Pedido</div><div class="value">#{pid}</div>
  <div class="label" style="margin-top:4px;">Cliente</div><div class="value">{cliente}</div>
  <div class="label" style="margin-top:4px;">Fecha</div><div class="value">{fecha_str}</div>
  <div class="estado-badge">{estado_str}</div>
  <div class="divider"></div>
  <div class="label">Items</div>
  {lineas_items}
  <div class="divider"></div>
  <div class="total-row">TOTAL: ${total_fmt}</div>
  <div class="divider"></div>
  <div class="footer">--- Control de Cocina ---</div>
</body></html>"""


# ── Impresión bajo demanda (P3) ─────────────────────────────────────────────────
def _emit_print(ticket_html: str, tid: int) -> None:
    """Emite UN solo iframe de impresión (antes había uno por tarjeta → decenas
    de iframes en un tablero ocupado). Intenta abrir la ventana automáticamente;
    si el navegador bloquea los popups, deja un botón para imprimir con un clic.
    """
    escaped = (ticket_html.replace("\\", "\\\\").replace("`", "\\`")
               .replace("${", "\\${").replace("</script>", "<" + "/script>"))
    fn = f"imprimir_{tid}"
    st.components.v1.html(f"""
    <div style="font-family:'DM Sans',sans-serif; font-size:0.8rem; color:#6b7280; display:flex; align-items:center; gap:10px;">
      <span id="msg_{tid}">🖨 Abriendo impresión del ticket #{tid}…</span>
      <button onclick="{fn}()" style="padding:6px 14px; background:#1a1a1a; color:#fff; border:none; border-radius:8px; font-family:'DM Sans',sans-serif; font-size:0.78rem; cursor:pointer;">Imprimir #{tid}</button>
    </div>
    <script>
      function {fn}() {{
        var w = window.open('', '_blank', 'width=320,height=500,scrollbars=yes');
        if (!w) {{
          document.getElementById('msg_{tid}').textContent =
            'Permite las ventanas emergentes y toca Imprimir.';
          return;
        }}
        w.document.write(`{escaped}`); w.document.close(); w.focus();
        setTimeout(function() {{ w.print(); w.close(); }}, 300);
      }}
      {fn}();  // intento automático (funciona si los popups están permitidos)
    </script>
    """, height=44)


def _maybe_print_ticket(df: pd.DataFrame) -> None:
    """Si una tarjeta pidió imprimir (print_ticket_id), genera y emite el ticket."""
    tid = st.session_state.pop("print_ticket_id", None)
    if tid is None:
        return
    match = df[df["id"] == tid]
    if match.empty:
        return
    r = match.iloc[0]
    ticket_html = generar_ticket_html(
        int(r["id"]), r.get("numero_cliente", "—"), r.get("items", []),
        r.get("total", 0), formatear_fecha(r.get("fecha")), r.get("estado", "pendiente"),
    )
    _emit_print(ticket_html, int(tid))


# ── Modal de cancelación (Fase 3) ───────────────────────────────────────────────
# @st.dialog abre un pop-up centrado en lugar del aviso inline que empujaba el
# layout. Se comparte entre el tablero y el monitor de mesas (claves por 'uid').
@st.dialog("Cancelar pedido")
def dialog_cancelar(pid: int, uid: str):
    pid = int(pid)
    st.markdown(
        f"¿Seguro que quieres **cancelar el pedido #{pid}**?  \n"
        "Esta acción no se puede deshacer."
    )
    motivo = st.text_input(
        "Motivo (opcional)", key=f"motivo_{uid}",
        placeholder="Ej: cliente se retiró, error de cocina…",
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✕ Sí, cancelar", key=f"confirm_cancel_{uid}", type="primary",
                     use_container_width=True):
            cancelar_pedido(pid, motivo)   # flashea toast + st.rerun()
    with c2:
        if st.button("Volver", key=f"volver_cancel_{uid}", use_container_width=True):
            st.rerun()


# ── Modal de cobro: efectivo/transferencia y abonos parciales (Fase: pagos) ──────
# Pop-up centrado compartido entre el tablero y el monitor de mesas
# (pedidos.dialog_cobrar). 'ids' = pedidos a cobrar (uno o varios); 'titulo' = mesa
# o pedido; 'total' = saldo pendiente total (lo calcula la vista con db.saldo_pedido).
@st.dialog("💵 Cobrar")
def dialog_cobrar(ids, titulo, total, uid):
    # Candado de capacidad (RBAC): el rol mesero NO cobra. La interfaz de cobro no
    # debe instanciarse aunque se alcance este modal por una ruta inesperada.
    if not auth.can("cobrar"):
        st.error("🔒 No tienes permiso para cobrar.")
        return
    ids = [int(i) for i in ids]
    total = max(0, int(total))

    # Anti-skimming: abrir el checkout SELLA cobro_iniciado (bloquea cancelar). Se hace una
    # sola vez por apertura del modal (flag de sesión) para no escribir en cada rerun de
    # tecla — el modal se re-ejecuta con cada number_input y no debe pegarle a la BD por tecla.
    _lock_key = f"_cobro_lock_{uid}"
    if not st.session_state.get(_lock_key):
        marcar_cobro_iniciado(ids)
        st.session_state[_lock_key] = True

    st.markdown(f"**{html.escape(str(titulo))}**")
    st.markdown(
        '<div style="font-size:0.78rem; color:#6b7280; text-transform:uppercase; '
        'letter-spacing:0.04em;">Total por pagar</div>'
        f'<div style="font-family:\'Syne\',sans-serif; font-size:1.9rem; font-weight:800; '
        f'color:#1a1a1a; line-height:1.1;">${fmt_money(total)}</div>',
        unsafe_allow_html=True,
    )

    if total <= 0:
        st.info("Esta cuenta ya está saldada.")
        if st.button("Cerrar", key=f"volver_cobrar_{uid}", use_container_width=True):
            st.rerun()
        return

    st.markdown("<br>", unsafe_allow_html=True)

    # Método de pago. El CSS global pinta el st.radio horizontal como píldoras
    # (segmented-control) y oculta su label → ponemos uno propio con st.caption.
    st.caption("Método de pago")
    metodo = st.radio(
        "Método de pago", ["💵 Efectivo", "💳 Transferencia"],
        horizontal=True, label_visibility="collapsed", key=f"metodo_{uid}",
    )
    es_efectivo = metodo == "💵 Efectivo"

    # Transferencia: el tender de efectivo NO aplica. Limpiamos su estado para que un
    # valor "insuficiente" previo no quede bloqueando el cobro ni reaparezca al volver
    # a Efectivo; y la validación de abajo ignora el cálculo de cambio (solo exige
    # abono > 0). Seguro: el widget 'recibe' solo se instancia en la rama de efectivo,
    # así que aquí (transferencia) podemos limpiar su clave sin chocar con el widget.
    submetodo_val, comprobante_val = None, None
    if not es_efectivo:
        st.session_state.pop(f"recibe_{uid}", None)
        # Detalle de la transferencia: billetera (Nequi/Daviplata/Bre-B) + comprobante.
        # El comprobante vive solo en este cobro de caja (no en la app pública del cliente).
        _SUBMETODOS = {"Nequi": "nequi", "Daviplata": "daviplata", "Bre-B": "breb"}
        st.caption("Billetera / canal")
        sub_label = st.radio(
            "Billetera", list(_SUBMETODOS.keys()),
            horizontal=True, label_visibility="collapsed", key=f"submetodo_{uid}",
        )
        submetodo_val = _SUBMETODOS.get(sub_label)
        comprobante_val = (st.text_input(
            "N.º de comprobante", key=f"comprobante_{uid}",
            placeholder="Referencia de la transacción (opcional)",
        ) or "").strip() or None

    # ── Modo de cobro: por MONTO o POR PLATO ────────────────────────────────────
    # 'Por plato' cobra unidades concretas (ej: 1 de 2 Coca-Colas). Se ofrece solo con
    # UN pedido y si el valor de las unidades pendientes cuadra con el saldo: si hubo un
    # abono por monto suelto (o el total lleva recargo de envío), los dos libros no
    # cuadrarían, así que se oculta para no descuadrar. Ver registrar_pago_items.
    lineas = lineas_pagables(ids[0]) if len(ids) == 1 else []
    precio_idx = {l["idx"]: l["precio"] for l in lineas}
    valor_items = sum(l["restante"] * l["precio"] for l in lineas)
    por_plato_ok = (len(ids) == 1 and any(l["restante"] > 0 for l in lineas)
                    and valor_items == total)

    por_plato = False
    if por_plato_ok:
        st.caption("Modo de cobro")
        modo = st.radio("Modo de cobro", ["💵 Monto", "🍽️ Por plato"],
                        horizontal=True, label_visibility="collapsed", key=f"modo_{uid}")
        por_plato = modo == "🍽️ Por plato"

    seleccion = {}
    if por_plato:
        st.markdown('<div style="font-size:0.82rem; color:#374151; margin:6px 0 2px;">'
                    'Unidades a cobrar</div>', unsafe_allow_html=True)
        for l in lineas:
            idx = l["idx"]
            ppkey = f"pp_{uid}_{idx}"
            restante = int(l["restante"])
            col_n, col_m, col_c, col_p = st.columns([4, 1, 1, 1])
            with col_n:
                if restante <= 0:
                    st.markdown(f'<div style="font-size:0.85rem; color:#9ca3af; padding:8px 0;">'
                                f'✓ {html.escape(str(l["nombre"]))} · pagado</div>',
                                unsafe_allow_html=True)
                else:
                    st.markdown(f'<div style="font-size:0.85rem; color:#1a1a1a; padding:4px 0;">'
                                f'{html.escape(str(l["nombre"]))}<br>'
                                f'<span style="font-size:0.76rem; color:#6b7280;">'
                                f'${fmt_money(l["precio"])} c/u · quedan {restante}</span></div>',
                                unsafe_allow_html=True)
            if restante <= 0:
                st.session_state.pop(ppkey, None)   # línea ya pagada: sin selector ni estado
                continue
            # Stepper −/+ (mismo estilo que el resto de la app, en vez de digitar el número).
            # OJO: NADA de st.rerun() aquí — dentro de un st.dialog cerraría el modal; el click
            # del botón ya re-ejecuta solo el cuerpo del diálogo. La cuenta se pinta DESPUÉS de
            # ambos botones (col_c se escribe al final) para que refleje el valor recién tocado.
            cur = max(0, min(int(st.session_state.get(ppkey, 0) or 0), restante))
            with col_m:
                if st.button("−", key=f"ppm_{uid}_{idx}", use_container_width=True,
                             disabled=cur <= 0):
                    st.session_state[ppkey] = max(0, cur - 1)
            with col_p:
                if st.button("+", key=f"ppp_{uid}_{idx}", use_container_width=True,
                             disabled=cur >= restante):
                    st.session_state[ppkey] = min(restante, cur + 1)
            with col_c:
                val = max(0, min(int(st.session_state.get(ppkey, 0) or 0), restante))
                st.markdown(f'<div style="text-align:center; padding:6px 0; font-weight:700;">'
                            f'{val}</div>', unsafe_allow_html=True)
            if val > 0:
                seleccion[idx] = val
        abono = sum(q * precio_idx.get(idx, 0) for idx, q in seleccion.items())
        st.markdown(
            '<div style="display:flex; justify-content:space-between; padding:8px 0 2px; '
            'border-top:1px solid #e5e7eb; margin-top:6px;"><span style="font-weight:600; '
            'color:#374151;">Subtotal seleccionado</span>'
            f'<span style="font-family:\'Syne\',sans-serif; font-weight:800; color:#1a1a1a;">'
            f'${fmt_money(abono)}</span></div>',
            unsafe_allow_html=True,
        )
    else:
        # Monto a abonar: por defecto el total (cobro completo). Reducirlo = abono
        # parcial. min_value=0 bloquea negativos; max_value=total impide sobre-cobrar.
        abono = int(st.number_input(
            "Monto a abonar", min_value=0, max_value=total, value=total, step=1000,
            format="%d", key=f"abono_{uid}",
            help="Por defecto cobra el total. Reduce el monto para registrar un abono parcial.",
        ) or 0)

    # Efectivo: cuánto entrega el cliente → cambio = entregado − abono (nunca < 0).
    efectivo_corto = False
    if es_efectivo:
        recibe = int(st.number_input(
            "¿Con cuánto paga el cliente?", min_value=0, value=abono, step=1000,
            format="%d", key=f"recibe_{uid}",
        ) or 0)
        if recibe >= abono:
            st.markdown(
                '<div style="background:#dcfce7; border:1px solid #86efac; border-radius:10px; '
                'padding:10px 14px; margin-top:6px; display:flex; justify-content:space-between; '
                'align-items:center;"><span style="color:#14532d; font-weight:600;">Cambio</span>'
                f'<span style="font-family:\'Syne\',sans-serif; font-weight:800; font-size:1.2rem; '
                f'color:#14532d;">${fmt_money(recibe - abono)}</span></div>',
                unsafe_allow_html=True,
            )
        else:
            # No mostramos cambio negativo: el efectivo no alcanza para el abono.
            efectivo_corto = True
            st.markdown(
                '<div style="background:#fef3c7; border:1px solid #fcd34d; border-radius:10px; '
                'padding:10px 14px; margin-top:6px; color:#92400e; font-weight:600;">'
                f'Efectivo insuficiente: faltan ${fmt_money(abono - recibe)} para abonar '
                f'${fmt_money(abono)}.</div>',
                unsafe_allow_html=True,
            )

    if abono < total and not efectivo_corto:
        st.markdown(
            f'<div style="font-size:0.8rem; color:#1e3a8a; margin-top:8px;">Abono parcial · '
            f'saldo restante: <b>${fmt_money(total - abono)}</b> (la cuenta sigue abierta).</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        # Transferencia se confirma solo con abono > 0 (sin cálculo de cambio); en
        # efectivo además exige que el tender alcance (no efectivo_corto).
        if st.button("✓ Confirmar pago", key=f"confirm_cobrar_{uid}", type="primary",
                     use_container_width=True,
                     disabled=(abono <= 0 or (es_efectivo and efectivo_corto))):
            metodo_pago = "efectivo" if es_efectivo else "transferencia"
            if por_plato:
                # Cobra las unidades elegidas (también suma a pago_lineas). El subtotal
                # ya es 'abono' (= valor de lo seleccionado).
                registrar_pago_items(ids[0], seleccion, metodo_pago,
                                     submetodo=submetodo_val, comprobante=comprobante_val)
            else:
                registrar_pago(ids, abono, metodo_pago,
                               submetodo=submetodo_val, comprobante=comprobante_val)
            # Cobro commiteado → encolamos el ticket. abrir_cajon lo decide el helper
            # (solo en efectivo). 'recibe' solo existe en la rama de efectivo.
            enqueue_recibo(ids, titulo, total, abono, metodo_pago,
                           recibido=recibe if es_efectivo else None,
                           submetodo=submetodo_val, comprobante=comprobante_val)
            # Libro mayor: el cobro queda atribuido al cajero (base del informe de personal).
            # 'monto' = lo abonado ahora; lo agrega reporte_personal por actor.
            audit.registrar("cobrar", "pedido", ids[0], {
                "ids": ids, "titulo": str(titulo), "monto": int(abono),
                "metodo": metodo_pago, "submetodo": submetodo_val,
                "saldo_antes": int(total), "saldo_despues": int(max(0, total - abono)),
                "por_plato": bool(por_plato),
            })
            st.session_state.pop(f"_cobro_lock_{uid}", None)
            if abono >= total:
                flash(f"Pago completo · {titulo} · ${fmt_money(total)}", "💵")
            else:
                flash(f"Abono registrado · {titulo} · saldo ${fmt_money(total - abono)}", "🧾")
            st.rerun()
    with c2:
        if st.button("Cancelar", key=f"volver_cobrar_{uid}", use_container_width=True):
            st.rerun()


# ── Modal de descuento / cortesía (gated por PIN de admin) ──────────────────────
@st.dialog("🏷️ Descuento / Cortesía")
def dialog_descuento(pedido_id: int, saldo: int, uid: str):
    # Capacidad base (cobrar) + autorización fuerte por PIN de admin dentro del modal.
    if not auth.can("cobrar"):
        st.error("🔒 No tienes permiso.")
        return
    pedido_id, saldo = int(pedido_id), int(saldo)
    st.markdown(f"Saldo actual del pedido **#{pedido_id}**: "
                f"<b style='color:#1a1a1a;'>${fmt_money(saldo)}</b>", unsafe_allow_html=True)
    if saldo <= 0:
        st.info("Este pedido no tiene saldo por descontar.")
        if st.button("Cerrar", key=f"desc_cerrar_{uid}", use_container_width=True):
            st.rerun()
        return

    st.caption("Tipo de ajuste")
    _TIPOS = {"💲 Monto fijo": "monto", "％ Porcentaje": "porcentaje",
              "🎁 Cortesía (comp. total)": "cortesia"}
    tipo_label = st.radio("Tipo", list(_TIPOS.keys()), horizontal=True,
                          label_visibility="collapsed", key=f"desc_tipo_{uid}")
    tipo = _TIPOS[tipo_label]

    if tipo == "monto":
        valor = int(st.number_input("Monto a descontar ($)", min_value=0, max_value=saldo,
                                    value=0, step=1000, format="%d", key=f"desc_val_{uid}") or 0)
    elif tipo == "porcentaje":
        valor = int(st.number_input("Porcentaje (%)", min_value=0, max_value=100, value=0,
                                    step=5, format="%d", key=f"desc_pct_{uid}") or 0)
        st.caption(f"≈ −${fmt_money(round(saldo * valor / 100))} sobre el saldo actual.")
    else:
        st.info(f"Cortesía: se comparán los ${fmt_money(saldo)} restantes (saldo → $0).")
        valor = saldo

    motivo = st.text_input("Justificación (obligatoria)", key=f"desc_motivo_{uid}",
                           placeholder="Ej: cortesía gerencia, demora en cocina, queja…")
    st.caption("🔐 Requiere PIN de administrador. Queda registrado en la auditoría a su nombre.")
    admin_pin = st.text_input("PIN de administrador", type="password", key=f"desc_pin_{uid}")

    falta_valor = (tipo != "cortesia" and valor <= 0)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🏷️ Aplicar", key=f"desc_apply_{uid}", type="primary",
                     use_container_width=True,
                     disabled=(not motivo.strip() or falta_valor)):
            autoriza = empleados.admin_pin_valido(admin_pin)
            if not autoriza:
                st.error("PIN de administrador inválido.")
            else:
                ok, msg = aplicar_descuento(pedido_id, tipo, valor, motivo, autoriza)
                if ok:
                    flash(msg, "🏷️")
                    st.rerun()
                else:
                    st.error(msg)
    with c2:
        if st.button("Cancelar", key=f"desc_volver_{uid}", use_container_width=True):
            st.rerun()


def render_pedidos(dataframe: pd.DataFrame, tab_key: str = "all", mesa_nombres=None):
    # Panel derecho de SOLO LECTURA: el tablero es una vista en vivo ("qué está pasando").
    # Las acciones (avanzar / cobrar / ticket / comanda / cancelar) viven en el Monitor de
    # mesas y en Nuevo pedido, no aquí — por eso ya no se pintan botones por tarjeta.
    if dataframe.empty:
        st.markdown('<p style="color:#9ca3af; font-size:0.85rem; padding:1rem 0;">Sin pedidos en esta categoría.</p>', unsafe_allow_html=True)
        return
    for _, row in dataframe.iterrows():
        pid     = row["id"]
        estado  = row.get("estado", "pendiente")
        emoji, etiqueta = icono_cliente(row, mesa_nombres)   # U6
        items   = formatear_items(row.get("items", []))
        total_p = row.get("total", 0)
        fecha   = formatear_fecha(row.get("fecha"))

        # U2: acento de urgencia por tiempo de espera (solo pedidos activos)
        mins        = minutos_espera(row.get("fecha"))
        color_urg   = urgencia(mins, estado)
        borde       = f' style="border-left:4px solid {color_urg};"' if color_urg else ""
        chip_espera = (f'<div style="font-size:0.72rem; color:{color_urg}; font-weight:700; '
                       f'margin-top:6px; white-space:nowrap;">⏱ {mins} min</div>') if color_urg else ""

        st.markdown(f"""
        <div class="order-card"{borde}>
          <div style="display:flex; justify-content:space-between; align-items:flex-start;">
            <div>
              <div class="order-id">Pedido #{pid}</div>
              <div class="order-num">{emoji} {html.escape(str(etiqueta))}</div>
              <div class="order-items">{items}</div>
              <div class="order-fecha">{fecha}</div>
            </div>
            <div style="text-align:right;">{badge_html(estado)}<div class="order-total" style="margin-top:8px;">${fmt_money(total_p)}</div>{chip_espera}</div>
          </div>
        </div>
        """, unsafe_allow_html=True)


# ── Agrupación por mesa (Req 3) ────────────────────────────────────────────────
@st.cache_data(ttl=60)  # P1: el mapa de mesas cambia poco; TTL corto basta
def cargar_mesas_nombres() -> dict:
    """Mapa {id: nombre} de mesas para etiquetar los grupos (tolerante a fallos)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, nombre FROM mesas")).mappings().all()
        return {int(r["id"]): r["nombre"] for r in rows}
    except Exception:
        return {}

def grupo_de_mesa(row, mesa_nombres: dict) -> str:
    """Etiqueta de grupo de un pedido: mesa real (mesa_id), 'Mesa N' heredada en
    numero_cliente, o 'Sin mesa' para pedidos de WhatsApp / sin asignar."""
    mid = row.get("mesa_id")
    if mid is not None and not pd.isna(mid):
        return mesa_nombres.get(int(mid), f"Mesa {int(mid)}")
    cliente = str(row.get("numero_cliente", "") or "").strip()
    if cliente.lower().startswith("mesa"):
        return cliente
    return "Sin mesa"

def _con_saldo_mask(df: pd.DataFrame) -> pd.Series:
    """Serie booleana de pedidos con SALDO PENDIENTE (> 0): excluye los pagados
    (pagado=TRUE) y los de saldo cero (total − total_pagado <= 0). Equivale a
    saldo_pedido(row) > 0 pero vectorizado. Defensiva ante columnas faltantes
    pre-migración (se tratan como FALSE / 0).

    Es la definición de 'activo' que comparten el tablero por estado, sus cuentas y
    la vista por mesa: un pedido cobrado (completo o de saldo cero) NO debe seguir en
    el tablero activo, igual que en el Monitor (cobrar = pedido resuelto)."""
    idx = df.index
    pagado = (df["pagado"].fillna(False).astype(bool) if "pagado" in df.columns
              else pd.Series(False, index=idx))
    total = (pd.to_numeric(df["total"], errors="coerce").fillna(0)
             if "total" in df.columns else pd.Series(0, index=idx))
    abonado = (pd.to_numeric(df["total_pagado"], errors="coerce").fillna(0)
               if "total_pagado" in df.columns else pd.Series(0, index=idx))
    return (~pagado) & ((total - abonado) > 0)


def render_por_mesa(df: pd.DataFrame, mesa_nombres: dict):
    """Agrupa y renderiza los pedidos ACTIVOS por mesa.

    'Activo' aquí = en cocina (pendiente/en preparación/listo) y CON saldo pendiente,
    igual que en el Monitor: cobrar libera la mesa, así que un pedido ya saldado no
    debe seguir apareciendo aunque su estado de cocina no se haya avanzado a
    'entregado'. Ver _con_saldo_mask (excluye pagado=TRUE o saldo <= 0)."""
    activos = df[df["estado"].isin(ESTADOS_ACTIVOS) & _con_saldo_mask(df)].copy()
    if activos.empty:
        st.markdown('<p style="color:#9ca3af; font-size:0.85rem; padding:1rem 0;">No hay pedidos activos en este momento.</p>', unsafe_allow_html=True)
        return
    activos["__grupo"] = activos.apply(lambda r: grupo_de_mesa(r, mesa_nombres), axis=1)
    # Mesas reales primero (por nombre); 'Sin mesa' al final.
    grupos = sorted(activos["__grupo"].unique(), key=lambda g: (g == "Sin mesa", str(g)))
    for gi, grupo in enumerate(grupos):
        sub = activos[activos["__grupo"] == grupo].copy()
        total_grupo = sub["total"].sum() if "total" in sub.columns else 0
        st.markdown(
            f'<div class="section-title">🪑 {html.escape(str(grupo))} · {len(sub)} activo(s) · ${fmt_money(total_grupo)}</div>',
            unsafe_allow_html=True,
        )
        render_pedidos(sub, tab_key=f"mesa{gi}", mesa_nombres=mesa_nombres)


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: PEDIDOS
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Refresco en vivo: SOLO este fragmento se re-ejecuta en el intervalo (no toda
    # la app ni panel.py), a diferencia del antiguo st_autorefresh → menos parpadeo
    # y menos carga. Las acciones (avanzar/cobrar/cancelar) siguen llamando st.rerun()
    # (scope app) para refrescar todo y vaciar los toasts en panel.py.
    _tablero_en_vivo()


@st.fragment(run_every="30s")
def _tablero_en_vivo():
    df = cargar_pedidos()

    # ── Audio alert: detect new pending orders ─────────────────────────────────
    pending_ids = set(df[df["estado"] == "pendiente"]["id"].tolist())

    if "known_pending_ids" not in st.session_state:
        # First load — seed silently, don't play sound
        st.session_state["known_pending_ids"] = pending_ids
        play_alert = False
    else:
        new_orders = pending_ids - st.session_state["known_pending_ids"]
        play_alert = len(new_orders) > 0
        st.session_state["known_pending_ids"] = pending_ids

    if play_alert:
        # U5: campana sintetizada con Web Audio — sin depender de URLs externas
        # (antes soundjay/freesound podían dar 404/CORS y dejar la cocina sin aviso).
        st.components.v1.html("""
        <script>
        (function(){
          try {
            var AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return;
            var ctx = new AC();
            function tono(freq, inicio, dur){
              var o = ctx.createOscillator(), g = ctx.createGain();
              o.connect(g); g.connect(ctx.destination);
              o.type = 'sine'; o.frequency.value = freq;
              var t = ctx.currentTime + inicio;
              g.gain.setValueAtTime(0.0001, t);
              g.gain.exponentialRampToValueAtTime(0.5, t + 0.03);
              g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
              o.start(t); o.stop(t + dur + 0.02);
            }
            var sonar = function(){ tono(880, 0, 0.25); tono(1175, 0.18, 0.38); };
            if (ctx.state === 'suspended') { ctx.resume().then(sonar).catch(function(){}); }
            else { sonar(); }
          } catch(e) {}
        })();
        </script>
        """, height=0)

    # 'activo' = pedido con saldo pendiente (no pagado y total − total_pagado > 0),
    # la misma definición que usa el Monitor: cobrar = pedido resuelto. Las cuentas y
    # pestañas de cocina (pendientes/en prep/listos) excluyen los ya saldados; 'Todos'
    # los conserva como catálogo. Ver _con_saldo_mask.
    activo = _con_saldo_mask(df)

    total      = len(df)
    pend       = len(df[(df["estado"] == "pendiente")      & activo])
    en_prep    = len(df[(df["estado"] == "en preparacion") & activo])
    listos     = len(df[(df["estado"] == "listo")          & activo])
    entregados = len(df[df["estado"] == "entregado"])
    cancelados = len(df[df["estado"] == "cancelado"]) if "cancelado" in df["estado"].values else 0
    # Ventas = dinero realmente cobrado, no solo entregado. Suma lo COBRADO de cada
    # pedido no cancelado de hoy (cobrado_pedido = total si pagado, si no el abono
    # parcial total_pagado) → incluye las cuentas parciales abiertas. Defensivo ante
    # columnas faltantes pre-migración (cobrado_pedido las trata como 0/FALSE).
    hoy_df = df[
        (pd.to_datetime(df["fecha"]).dt.date == datetime.now().date()) &
        (df["estado"] != "cancelado")
    ]
    ventas_hoy = int(hoy_df.apply(cobrado_pedido, axis=1).sum()) if not hoy_df.empty else 0

    # Tira compacta de stats: el tablero vive ahora en un panel lateral angosto, así
    # que las 5 metric-cards grandes partían el texto en vertical ("PEN/DIE/NTES").
    # Una tira flex con números pequeños cabe sin romperse y prioriza la cola de
    # cocina (pendientes · en prep · listos) + lo cobrado hoy. 'Total pedidos' se
    # quitó por ser el dato menos accionable en vivo (sigue en la pestaña "Todos").
    st.markdown(
        '<div class="ped-stats">'
        f'<div class="ped-stat"><span class="ped-stat-n metric-accent">{pend}</span>'
        '<span class="ped-stat-l">Pendientes</span></div>'
        f'<div class="ped-stat"><span class="ped-stat-n metric-blue">{en_prep}</span>'
        '<span class="ped-stat-l">En prep.</span></div>'
        f'<div class="ped-stat"><span class="ped-stat-n metric-green">{listos}</span>'
        '<span class="ped-stat-l">Listos</span></div>'
        f'<div class="ped-stat"><span class="ped-stat-n">${fmt_money(ventas_hoy)}</span>'
        '<span class="ped-stat-l">Ventas hoy</span></div>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)

    # P3: emite el ticket pedido por una tarjeta (un único iframe, no uno por orden).
    _maybe_print_ticket(df)

    # Req 3: alterna entre el tablero agrupado por mesa y la vista por estado.
    vista = st.radio(
        "Vista", ["🪑 Por mesa", "📋 Por estado"],
        horizontal=True, label_visibility="collapsed", key="vista_pedidos"
    )
    st.markdown("<br>", unsafe_allow_html=True)

    mesa_nombres = cargar_mesas_nombres()  # U6/P1: una sola lectura cacheada

    if vista == "🪑 Por mesa":
        render_por_mesa(df, mesa_nombres)
    else:
        tab_todos, tab_pend, tab_prep, tab_listo, tab_entregado, tab_cancelado = st.tabs([
            f"Todos ({total})",
            f"Pendientes ({pend})",
            f"En preparación ({en_prep})",
            f"Listos ({listos})",
            f"Entregados ({entregados})",
            f"Cancelados ({cancelados})"
        ])

        with tab_todos:
            render_pedidos(df, "todos", mesa_nombres=mesa_nombres)
        with tab_pend:
            render_pedidos(df[(df["estado"] == "pendiente") & activo].copy(), "pend", mesa_nombres=mesa_nombres)
        with tab_prep:
            render_pedidos(df[(df["estado"] == "en preparacion") & activo].copy(), "prep", mesa_nombres=mesa_nombres)
        with tab_listo:
            render_pedidos(df[(df["estado"] == "listo") & activo].copy(), "listo", mesa_nombres=mesa_nombres)
        with tab_entregado:
            render_pedidos(df[df["estado"] == "entregado"].copy(), "entregado", mesa_nombres=mesa_nombres)
        with tab_cancelado:
            render_pedidos(df[df["estado"] == "cancelado"].copy(), "cancelado", mesa_nombres=mesa_nombres)

    st.markdown("<br>", unsafe_allow_html=True)
    # Vista de solo lectura: sin botón de refresco manual (el fragmento se actualiza
    # solo cada 30 s). Las acciones se gestionan desde el Monitor de mesas.
    st.markdown('<p style="color:#9ca3af; font-size:0.75rem;">Vista en vivo · se actualiza '
                'automáticamente. Gestiona los pedidos desde el 🖥️ Monitor.</p>',
                unsafe_allow_html=True)
