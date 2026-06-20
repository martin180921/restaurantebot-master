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
                   comprobante: str | None = None) -> None:
    """Encola el ticket de un cobro recién commiteado.

    Regla del cajón SAT: solo se abre en efectivo (abrir_cajon = metodo=='efectivo').
    'submetodo'/'comprobante' detallan la transferencia (Nequi/Daviplata/Bre-B + n.º de
    transacción) en el ticket. Tolera cualquier fallo: la impresión no debe tumbar el
    cobro ya registrado.
    """
    try:
        saldo = max(0, int(total) - int(abono))
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
            "abrir_cajon": metodo == "efectivo",
            "pedido_ids": [int(i) for i in ids],
        }
        enqueue_job(RESTAURANTE_ID, "recibo", payload)
    except Exception:
        # El cobro ya está en la BD; un fallo de encolado no debe propagarse a la UI.
        pass


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
