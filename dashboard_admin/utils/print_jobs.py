"""Encolado de trabajos de impresión hacia la cola multi-tenant `print_jobs`.

El panel (cloud) NO habla con la impresora: solo inserta filas 'pendiente' en la
BD. El Agente de Impresión Local de cada restaurante (print_agent/) hace polling y
las imprime. Así el split cloud↔local queda desacoplado y con reintentos/auditoría.
"""
import json

from sqlalchemy import text

from db import engine, RESTAURANTE_ID
from utils.items import items_para_ticket, parse_items


def enqueue_job(restaurante_id: int, tipo: str, payload: dict) -> int:
    """Inserta un trabajo de impresión y devuelve su id.

    'tipo' es libre pero hoy usamos 'recibo' (ticket de cobro). 'payload' se
    serializa a JSONB. Nunca lanza hacia la UI: si la cola falla, el cobro YA quedó
    commiteado, así que no debemos romper el flujo del cajero por un fallo de impresión.
    """
    sql = text(
        "INSERT INTO print_jobs (restaurante_id, tipo, payload) "
        "VALUES (:rid, :tipo, CAST(:payload AS JSONB)) RETURNING id"
    )
    with engine.begin() as conn:
        return int(conn.execute(sql, {
            "rid": int(restaurante_id),
            "tipo": tipo,
            "payload": json.dumps(payload, ensure_ascii=False),
        }).scalar_one())


def _items_payload(ids) -> list[dict]:
    """Items (modelo por secciones) agregados de los pedidos 'ids' para el ticket.

    Concatena los items de todos los pedidos y los normaliza con items_para_ticket:
    los Plato del Día van individuales (cada uno con su desglose en 'componentes'),
    los simples se agregan por nombre. Cada item conserva 'nombre'/'cantidad' para
    que un agente antiguo siga imprimiéndolos aunque ignore las categorías.
    """
    sql = text("SELECT items FROM pedidos WHERE id = ANY(:ids) ORDER BY id")
    concat: list[dict] = []
    with engine.connect() as conn:
        filas = conn.execute(sql, {"ids": [int(i) for i in ids]}).scalars().all()
    for raw in filas:
        concat.extend(parse_items(raw))
    return items_para_ticket(concat)


def enqueue_recibo(ids, titulo: str, total: int, abono: int, metodo: str,
                   recibido: int | None = None, submetodo: str | None = None,
                   comprobante: str | None = None, desglose: list | None = None) -> None:
    """Encola el ticket de un cobro recién commiteado.

    Regla del cajón SAT: solo se abre cuando hubo efectivo (abrir_cajon).
    'submetodo'/'comprobante' detallan la transferencia (Nequi/Daviplata/Bre-B + n.º de
    transacción) en el ticket.

    'desglose' (opcional) es la lista de tramos de un pago MIXTO
    [{metodo, monto, submetodo?, comprobante?}]: cuando una sola persona paga UNA cuenta
    repartiendo el monto entre efectivo y transferencia, se imprime UN solo ticket con
    ambos tramos en vez de dos recibos. Con desglose, 'metodo' global es 'mixto' y el
    cajón se abre si algún tramo fue en efectivo.

    Tolera cualquier fallo: la impresión no debe tumbar el cobro ya registrado.
    """
    try:
        saldo = max(0, int(total) - int(abono))
        desg = [d for d in (desglose or []) if int(d.get("monto") or 0) > 0]
        tiene_efectivo = (metodo == "efectivo") or any(
            str(d.get("metodo")) == "efectivo" for d in desg)
        payload = {
            "mesa": titulo,
            "items": _items_payload(ids),
            "total": int(total),
            "pagado": int(abono),
            "saldo": saldo,
            "metodo": metodo,
            "submetodo": submetodo or None,
            "comprobante": comprobante or None,
            "recibido": int(recibido) if recibido is not None else None,
            "cambio": max(0, int(recibido) - int(abono)) if recibido is not None else 0,
            "abrir_cajon": tiene_efectivo,
            "pedido_ids": [int(i) for i in ids],
        }
        if desg:
            tramos = []
            for d in desg:
                tramo = {"metodo": str(d.get("metodo")),
                         "monto": int(d.get("monto") or 0),
                         "submetodo": d.get("submetodo") or None,
                         "comprobante": d.get("comprobante") or None}
                # Tender del tramo en efectivo → Recibido/Cambio en el ticket.
                if d.get("recibido") is not None:
                    rec = int(d.get("recibido"))
                    tramo["recibido"] = rec
                    tramo["cambio"] = max(0, rec - int(tramo["monto"]))
                tramos.append(tramo)
            payload["desglose"] = tramos
        enqueue_job(RESTAURANTE_ID, "recibo", payload)
    except Exception:
        # El cobro ya está en la BD; un fallo de encolado no debe propagarse a la UI.
        pass


# ── Salud del Agente de Impresión Local (heartbeat) ─────────────────────────────
# El agente hace upsert de su latido en agentes_estado en cada ciclo de polling. Aquí
# lo leemos para que el panel muestre si está vivo y cuánto tiene en cola, en vez de
# enterarnos de que está caído porque un cliente se quedó sin recibo.
def estado_agente(restaurante_id=None) -> dict | None:
    """{online, visto_at, cola_pendiente, segundos} del agente local, o None si nunca
    latió. 'online' = visto en los últimos 30 s (el agente late cada ~2 s)."""
    rid = int(restaurante_id if restaurante_id is not None else RESTAURANTE_ID)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT visto_at, cola_pendiente, "
                "       EXTRACT(EPOCH FROM (NOW() - visto_at)) AS seg "
                "FROM agentes_estado WHERE restaurante_id = :r"
            ), {"r": rid}).mappings().first()
    except Exception:
        return None
    if not row:
        return None
    seg = int(row["seg"] or 0)
    return {"online": seg <= 30, "visto_at": row["visto_at"],
            "cola_pendiente": int(row["cola_pendiente"] or 0), "segundos": seg}


def badge_agente_html(estado: dict | None = "__auto__") -> str:
    """Badge HTML de salud del agente para incrustar en una vista (Caja/Monitor)."""
    if estado == "__auto__":
        estado = estado_agente()
    base = ("display:inline-block; padding:4px 12px; border-radius:999px; "
            "font-size:0.75rem; font-weight:600; border:1px solid;")
    if not estado:
        return (f'<span style="{base} background:#f3f4f6; color:#6b7280; border-color:#e5e7eb;">'
                '🖨️ Agente de impresión: sin datos</span>')
    cola = estado["cola_pendiente"]
    cola_txt = f' · {cola} en cola' if cola else ''
    if estado["online"]:
        return (f'<span style="{base} background:#dcfce7; color:#15803d; border-color:#bbf7d0;">'
                f'🟢 Agente en línea{cola_txt}</span>')
    mins = max(1, estado["segundos"] // 60)
    return (f'<span style="{base} background:#fee2e2; color:#b91c1c; border-color:#fecaca;">'
            f'🔴 Agente sin conexión (~{mins} min){cola_txt}</span>')


def enqueue_comanda(pedido_id: int) -> None:
    """Encola la comanda de cocina de un pedido (al pasar a 'en preparacion').

    Sin precios ni cajón: la cocina solo ve mesa/cliente + ítems. Tolera fallos:
    la impresión de cocina no debe romper el avance de estado del pedido.
    """
    try:
        sql = text("""
            SELECT p.numero_cliente, p.mesa_id, p.items, m.nombre AS mesa_nombre
            FROM pedidos p LEFT JOIN mesas m ON m.id = p.mesa_id
            WHERE p.id = :id
        """)
        with engine.connect() as conn:
            row = conn.execute(sql, {"id": int(pedido_id)}).mappings().first()
        if not row:
            return
        payload = {
            "pedido_id": int(pedido_id),
            "mesa": row["mesa_nombre"] or row["numero_cliente"] or f"Pedido #{pedido_id}",
            "items": items_para_ticket(parse_items(row["items"])),
            "abrir_cajon": False,
        }
        enqueue_job(RESTAURANTE_ID, "comanda", payload)
    except Exception:
        pass


def enqueue_prerecibo(pedido_id: int) -> None:
    """Encola el PRERECIBO (pre-cuenta) de un pedido — botón "🖨 Ticket".

    Es la cuenta que se entrega al cliente ANTES de pagar: mismos ítems que el recibo
    + total, encabezado PRERECIBO y la MESA de la que proviene. Sin pago ni cajón. El
    'pagado' lleva lo ya abonado (total_pagado) para mostrar el saldo si la cuenta es
    parcial. Tolera fallos: imprimir la pre-cuenta no debe romper el panel.
    """
    try:
        sql = text("""
            SELECT p.numero_cliente, p.mesa_id, p.items, p.total, p.total_pagado,
                   m.nombre AS mesa_nombre
            FROM pedidos p LEFT JOIN mesas m ON m.id = p.mesa_id
            WHERE p.id = :id
        """)
        with engine.connect() as conn:
            row = conn.execute(sql, {"id": int(pedido_id)}).mappings().first()
        if not row:
            return
        payload = {
            "pedido_id": int(pedido_id),
            "mesa": row["mesa_nombre"] or row["numero_cliente"] or f"Pedido #{pedido_id}",
            "items": items_para_ticket(parse_items(row["items"])),
            "total": int(row["total"] or 0),
            "pagado": int(row["total_pagado"] or 0),
            "abrir_cajon": False,
        }
        enqueue_job(RESTAURANTE_ID, "prerecibo", payload)
    except Exception:
        pass
