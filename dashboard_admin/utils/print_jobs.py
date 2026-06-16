"""Encolado de trabajos de impresión hacia la cola multi-tenant `print_jobs`.

El panel (cloud) NO habla con la impresora: solo inserta filas 'pendiente' en la
BD. El Agente de Impresión Local de cada restaurante (print_agent/) hace polling y
las imprime. Así el split cloud↔local queda desacoplado y con reintentos/auditoría.
"""
import json

from sqlalchemy import text

from db import engine, RESTAURANTE_ID


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


def _items_de_pedidos(ids) -> list[dict]:
    """[{nombre, cantidad}] agregados de los pedidos 'ids', para pintar el ticket."""
    sql = text("SELECT items FROM pedidos WHERE id = ANY(:ids) ORDER BY id")
    agregados: dict[str, int] = {}
    with engine.connect() as conn:
        filas = conn.execute(sql, {"ids": [int(i) for i in ids]}).scalars().all()
    for raw in filas:
        items = raw
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except (ValueError, TypeError):
                continue
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict):
                nombre = str(it.get("nombre", "?"))
                cant = int(it.get("cantidad", 1) or 1)
                agregados[nombre] = agregados.get(nombre, 0) + cant
    return [{"nombre": n, "cantidad": c} for n, c in agregados.items()]


def enqueue_recibo(ids, titulo: str, total: int, abono: int, metodo: str,
                   recibido: int | None = None) -> None:
    """Encola el ticket de un cobro recién commiteado.

    Regla del cajón SAT: solo se abre en efectivo (abrir_cajon = metodo=='efectivo').
    Tolera cualquier fallo: la impresión no debe tumbar el cobro ya registrado.
    """
    try:
        saldo = max(0, int(total) - int(abono))
        payload = {
            "mesa": titulo,
            "items": _items_de_pedidos(ids),
            "total": int(total),
            "pagado": int(abono),
            "saldo": saldo,
            "metodo": metodo,
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
        items = row["items"]
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except (ValueError, TypeError):
                items = []
        items_norm = [
            {"nombre": str(it.get("nombre", "?")), "cantidad": int(it.get("cantidad", 1) or 1)}
            for it in (items or []) if isinstance(it, dict)
        ]
        payload = {
            "pedido_id": int(pedido_id),
            "mesa": row["mesa_nombre"] or row["numero_cliente"] or f"Pedido #{pedido_id}",
            "items": items_norm,
            "abrir_cajon": False,
        }
        enqueue_job(RESTAURANTE_ID, "comanda", payload)
    except Exception:
        pass
