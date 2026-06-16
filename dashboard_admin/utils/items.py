"""Formateo compartido de los items de un pedido (modelo por secciones + legado).

Un elemento de `pedidos.items` (JSON) es uno de:
  - Plato del Día: {tipo:'plato_dia', nombre, precio, cantidad:1,
                    config:{entrada, principio, proteina, acompanamientos:[...]}, nota}
  - Especial / a la carta / bebida: {tipo:'especial'|'item'|'bebida', id, nombre, precio, cantidad}
  - Legado (sin 'tipo'): {id, nombre, precio, cantidad}  → se trata como item simple.

Lo usan el tablero (views/pedidos.py) y la cola de impresión (utils/print_jobs.py).
El Agente de Impresión Local (print_agent/) NO importa esto — son procesos aislados
que solo comparten el contrato de datos del payload — así que tiene su propia copia
mínima del render. Todo es retro-compatible: un item legado sin 'tipo' se imprime
igual que antes ('N x nombre'), sin cabecera de categoría ni desglose.
"""
import json
import html

# Cabecera de categoría para los tickets agrupados.
CAT_LABEL = {
    "plato_dia": "PLATO DEL DIA",
    "especial":  "ESPECIALES",
    "item":      "A LA CARTA",
    "bebida":    "BEBIDAS",
}
# Etiqueta de cada paso de configuración del Plato del Día.
GRUPO_LABEL = {
    "entrada":   "Entrada",
    "principio": "Principio",
    "proteina":  "Proteína",
    "acompanamiento": "Acompañamientos",
}
# Orden canónico de las categorías en cualquier ticket.
ORDEN_CAT = ["plato_dia", "especial", "item", "bebida"]


def parse_items(raw):
    """Normaliza el campo items (TEXT JSON o lista) a list[dict]. Tolera basura/strings."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [it for it in raw if isinstance(it, dict)]


def item_tipo(item) -> str:
    """Tipo del item; los legados sin 'tipo' se consideran 'item' (a la carta)."""
    t = str(item.get("tipo") or "item").lower()
    return t if t in ORDEN_CAT else "item"


def _agrupa_acomp(acomp) -> str:
    """['Arroz','Arroz','Maduro'] → '2x Arroz, 1x Maduro' (conserva el primer orden)."""
    if not isinstance(acomp, list):
        return ""
    orden, cuenta = [], {}
    for a in acomp:
        a = str(a)
        if a not in cuenta:
            orden.append(a)
        cuenta[a] = cuenta.get(a, 0) + 1
    return ", ".join(f"{cuenta[a]}x {a}" for a in orden)


def componentes_lineas(item):
    """[[etiqueta, valor], ...] de un plato_dia: entrada/principio/proteína/acomp +
    nota. Listas (no tuplas) para serializar limpio a JSON. [] si no es plato_dia."""
    if item_tipo(item) != "plato_dia":
        return []
    cfg = item.get("config") or {}
    out = []
    for g in ("entrada", "principio", "proteina"):
        v = cfg.get(g)
        if v:
            out.append([GRUPO_LABEL[g], str(v)])
    acomp = _agrupa_acomp(cfg.get("acompanamientos"))
    if acomp:
        out.append([GRUPO_LABEL["acompanamiento"], acomp])
    nota = str(item.get("nota") or "").strip()
    if nota:
        out.append(["Nota", nota])
    return out


def etiqueta_item(item) -> str:
    """Texto plano de UN item para resúmenes en línea (sin prefijo de cantidad). El
    Plato del Día incluye su desglose entre paréntesis."""
    nombre = str(item.get("nombre") or "?")
    if item_tipo(item) == "plato_dia":
        partes = [v for _, v in componentes_lineas(item)]
        if partes:
            return f"{nombre} ({' · '.join(partes)})"
    return nombre


def formatear_items_texto(raw) -> str:
    """Resumen en una línea: '1x Plato del Día (…), 2x Coca-Cola'. Sin HTML."""
    items = parse_items(raw)
    if not items:
        return ""
    return ", ".join(f"{int(it.get('cantidad', 1) or 1)}x {etiqueta_item(it)}" for it in items)


def formatear_items_html(raw) -> str:
    """formatear_items_texto escapado para inyectar en el HTML del tablero."""
    return html.escape(formatear_items_texto(raw))


def _agrupar(raw):
    """[(tipo, [{nombre, cantidad, componentes}, ...]), ...] en ORDEN_CAT. Los
    plato_dia van individuales (cada plato tiene su propia config); los simples se
    agregan por nombre dentro de su categoría."""
    items = parse_items(raw)
    buckets = {c: [] for c in ORDEN_CAT}
    indices = {}
    for it in items:
        tipo = item_tipo(it)
        qty = int(it.get("cantidad", 1) or 1)
        nombre = str(it.get("nombre") or "?")
        if tipo == "plato_dia":
            buckets[tipo].append({"nombre": nombre, "cantidad": qty,
                                  "componentes": componentes_lineas(it)})
        else:
            key = (tipo, nombre)
            if key in indices:
                indices[key]["cantidad"] += qty
            else:
                d = {"nombre": nombre, "cantidad": qty, "componentes": []}
                indices[key] = d
                buckets[tipo].append(d)
    return [(c, buckets[c]) for c in ORDEN_CAT if buckets[c]]


def lineas_por_categoria(raw):
    """[(CAT_LABEL, [{nombre, cantidad, componentes}, ...]), ...] para tickets
    agrupados (HTML del navegador)."""
    return [(CAT_LABEL[tipo], items) for tipo, items in _agrupar(raw)]


def items_para_ticket(raw):
    """Lista plana en orden de categoría, cada item con su 'tipo' y 'componentes'
    [[et,val],...], lista para el payload de print_jobs. Conserva 'nombre'/'cantidad'
    para que un agente antiguo (sin soporte de categorías) la siga imprimiendo."""
    flat = []
    for tipo, items in _agrupar(raw):
        for it in items:
            flat.append({"tipo": tipo, "nombre": it["nombre"],
                         "cantidad": it["cantidad"], "componentes": it["componentes"]})
    return flat
