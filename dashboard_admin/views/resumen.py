"""Vista de Resumen: cierre de caja y ventas por día (F2)."""
import streamlit as st
from sqlalchemy import text, bindparam
import pandas as pd
import json
from datetime import date

import auth
from db import engine, fmt_money, saldo_pedido, cobrado_pedido, titulo_seccion, hoy_bogota
from utils.items import formatear_items_texto


# ── DB ───────────────────────────────────────────────────────────────────────────
def cargar_pedidos_dia(dia: date):
    """Pedidos cuya fecha cae en el día indicado (compara la parte de fecha)."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, num_dia, numero_cliente, items, total, estado, fecha,
                   mesa_id, motivo_cancelacion, pagado, total_pagado,
                   tipo_entrega, cliente_nombre, metodo_pago, mesero, fee,
                   descuento_valor, tipo_descuento, motivo_descuento, descuento_autoriza
            FROM pedidos
            WHERE fecha::date = :dia
            ORDER BY fecha
        """), {"dia": dia}).mappings().all()
    return [dict(r) for r in rows]


def cargar_metodos_por_pedido(ids) -> dict:
    """{pedido_id: {metodo: monto}} desde el libro 'pagos', consultado por pedido (no por
    fecha de pago) para que el método del pedido sea correcto aunque se haya cobrado otro
    día. Tolerante: si la tabla aún no existe (pre-migración) devuelve {}."""
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT pedido_id, metodo, COALESCE(SUM(monto), 0) AS total "
                "FROM pagos WHERE pedido_id IN :ids GROUP BY pedido_id, metodo"
            ).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).mappings().all()
    except Exception:
        return {}
    out = {}
    for r in rows:
        out.setdefault(int(r["pedido_id"]), {})[r["metodo"]] = int(r["total"])
    return out


# Etiquetas de tipo de entrega para la tabla de pedidos del día.
_TIPO_LABEL = {
    "mesa":        "🪑 Mesa",
    "mesa_qr":     "🪑 Mesa (QR)",
    "domicilio":   "🛵 Domicilio",
    "para_llevar": "🛍️ Para llevar",
}


def _tipo_norm(tipo_entrega) -> str:
    """Normaliza tipo_entrega a una de las categorías de filtro. NULL/desconocido = mesa
    (los pedidos de salón heredados pueden tener tipo_entrega NULL)."""
    t = str(tipo_entrega or "").strip().lower()
    if t in ("domicilio", "para_llevar"):
        return t
    if t == "mesa_qr":
        return "mesa_qr"
    return "mesa"


def _metodo_label(metodos: dict) -> str:
    """Etiqueta del método de pago real de un pedido a partir de su(s) abono(s)."""
    if not metodos:
        return "—"
    efvo = metodos.get("efectivo", 0)
    transf = metodos.get("transferencia", 0)
    if efvo > 0 and transf > 0:
        return "Mixto"
    if efvo > 0:
        return "Efectivo"
    if transf > 0:
        return "Transferencia"
    return ", ".join(sorted(metodos.keys()))


def cargar_cobros_por_metodo(dia: date) -> dict:
    """{metodo: total} de los abonos del día desde el libro 'pagos' (por HORA REAL de
    pago, no por fecha del pedido). Tolerante a fallos: si la tabla aún no existe
    (pre-migración) o falla, devuelve {} → el desglose muestra $0 por método. El
    libro empieza a registrar desde el deploy, así que días previos saldrán vacíos."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT metodo, COALESCE(SUM(monto), 0) AS total "
                "FROM pagos WHERE fecha::date = :dia GROUP BY metodo"
            ), {"dia": dia}).mappings().all()
        return {r["metodo"]: int(r["total"]) for r in rows}
    except Exception:
        return {}


def _parse_items(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return []
    return raw or []


def _items_texto(raw) -> str:
    """Items legibles para el CSV: '2x Pizza, 1x Ensalada'."""
    partes = []
    for it in _parse_items(raw):
        if isinstance(it, dict):
            partes.append(f"{it.get('cantidad', 1)}x {it.get('nombre', '?')}")
        else:
            partes.append(str(it))
    return ", ".join(partes)


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: RESUMEN
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Candado de ingresos (RBAC): caja y mesero NO ven métricas de venta. El router
    # ni siquiera crea esta pestaña para esos roles; guard de defensa en profundidad.
    if not auth.can("see_revenue"):
        st.error("🔒 Acceso denegado")
        st.stop()
    st.markdown(titulo_seccion('📊 Resumen del día'), unsafe_allow_html=True)

    dia = st.date_input("Día", value=hoy_bogota(), format="DD/MM/YYYY", key="resumen_dia")

    pedidos = cargar_pedidos_dia(dia)
    if not pedidos:
        st.markdown('<p style="color:#a3a39b; font-size:0.9rem; padding:1rem 0;">No hay pedidos registrados en esta fecha.</p>', unsafe_allow_html=True)
        return

    df = pd.DataFrame(pedidos)
    # Cierre de caja = dinero realmente cobrado. Con pagos parciales:
    #   Cobrado    = Σ cobrado_pedido (total si pagado, si no el abono total_pagado)
    #   Por cobrar = Σ saldo_pedido   (total − abonos) → juntos parten el total del día.
    # Los conteos / ítems / por-hora siguen sobre pedidos cobrados POR COMPLETO
    # (transacciones cerradas). 'pagado' puede faltar pre-migración → tratado FALSE.
    pagado_col = (df["pagado"].fillna(False).astype(bool) if "pagado" in df.columns
                  else pd.Series(False, index=df.index))
    no_cancelados = df[df["estado"] != "cancelado"]
    pagados    = df[pagado_col & (df["estado"] != "cancelado")]   # cobrados por completo
    cancelados = df[df["estado"] == "cancelado"]

    cobrado    = int(no_cancelados.apply(cobrado_pedido, axis=1).sum()) if not no_cancelados.empty else 0
    por_cobrar = int(no_cancelados.apply(saldo_pedido, axis=1).sum()) if not no_cancelados.empty else 0
    n_ped      = len(pagados)
    n_canc     = len(cancelados)
    # Ticket promedio = valor medio de los pedidos cobrados por completo.
    ventas_completas = int(pagados["total"].sum()) if not pagados.empty else 0
    ticket     = ventas_completas / n_ped if n_ped else 0

    # ── Métricas (cierre de caja) ──────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-green" style="font-size:clamp(0.9rem,1.6vw,2rem); white-space:nowrap;">${fmt_money(cobrado)}</div><div class="metric-label">Cobrado</div></div>', unsafe_allow_html=True)
    with c2:
        st.markdown(f'<div class="metric-card"><div class="metric-value">{n_ped}</div><div class="metric-label">Pedidos cobrados</div></div>', unsafe_allow_html=True)
    with c3:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-blue" style="font-size:clamp(0.9rem,1.6vw,2rem); white-space:nowrap;">${fmt_money(ticket)}</div><div class="metric-label">Ticket promedio</div></div>', unsafe_allow_html=True)
    with c4:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-accent" style="font-size:clamp(0.9rem,1.6vw,2rem); white-space:nowrap;">${fmt_money(por_cobrar)}</div><div class="metric-label">Por cobrar</div></div>', unsafe_allow_html=True)
    with c5:
        st.markdown(f'<div class="metric-card"><div class="metric-value" style="color:#dc2626">{n_canc}</div><div class="metric-label">Cancelados</div></div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Desglose de cobros por método (libro 'pagos', por hora real de pago) ─────
    # Para cuadrar la caja: cuánto entró en efectivo vs transferencia. Es por hora de
    # pago (no por fecha del pedido), así que puede diferir levemente de "Cobrado".
    cobros = cargar_cobros_por_metodo(dia)
    efvo   = cobros.get("efectivo", 0)
    transf = cobros.get("transferencia", 0)
    otros  = sum(v for k, v in cobros.items() if k not in ("efectivo", "transferencia"))
    total_cobros = efvo + transf + otros
    st.markdown('<div class="section-title">Cobros por método</div>', unsafe_allow_html=True)
    d1, d2, d3 = st.columns(3)
    with d1:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-green" style="font-size:clamp(0.9rem,1.6vw,2rem); white-space:nowrap;">${fmt_money(efvo)}</div><div class="metric-label">💵 Efectivo</div></div>', unsafe_allow_html=True)
    with d2:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-blue" style="font-size:clamp(0.9rem,1.6vw,2rem); white-space:nowrap;">${fmt_money(transf)}</div><div class="metric-label">💳 Transferencia</div></div>', unsafe_allow_html=True)
    with d3:
        st.markdown(f'<div class="metric-card"><div class="metric-value" style="font-size:clamp(0.9rem,1.6vw,2rem); white-space:nowrap;">${fmt_money(total_cobros)}</div><div class="metric-label">Total cobros</div></div>', unsafe_allow_html=True)
    st.markdown('<p style="color:#a3a39b; font-size:0.72rem; margin-top:4px;">Por hora real de pago (libro de pagos). Empieza a registrar desde su activación; días anteriores pueden verse en $0.</p>', unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    col_items, col_horas = st.columns(2)

    # ── Más vendidos ───────────────────────────────────────────────────────────
    with col_items:
        st.markdown('<div class="section-title">Más vendidos</div>', unsafe_allow_html=True)
        agg = {}
        for _, r in pagados.iterrows():
            for it in _parse_items(r["items"]):
                if not isinstance(it, dict):
                    continue
                nombre = it.get("nombre", "?")
                qty    = int(it.get("cantidad", 1) or 1)
                precio = int(it.get("precio", 0) or 0)
                a = agg.setdefault(nombre, {"cantidad": 0, "importe": 0})
                a["cantidad"] += qty
                a["importe"]  += precio * qty
        if agg:
            tabla = pd.DataFrame([
                {"Plato": k, "Cant.": v["cantidad"], "Importe": f"${fmt_money(v['importe'])}"}
                for k, v in sorted(agg.items(), key=lambda kv: kv[1]["importe"], reverse=True)
            ])
            st.dataframe(tabla, hide_index=True, use_container_width=True)
        else:
            st.markdown('<p style="color:#a3a39b; font-size:0.85rem;">Sin platos vendidos.</p>', unsafe_allow_html=True)

    # ── Ventas por hora ────────────────────────────────────────────────────────
    with col_horas:
        st.markdown('<div class="section-title">Cobrado por hora</div>', unsafe_allow_html=True)
        if not pagados.empty:
            horas = pagados.copy()
            horas["hora"] = pd.to_datetime(horas["fecha"]).dt.hour
            serie = horas.groupby("hora")["total"].sum()
            serie = serie.reindex(range(int(serie.index.min()), int(serie.index.max()) + 1), fill_value=0)
            serie.index = [f"{h:02d}h" for h in serie.index]
            st.bar_chart(serie, color="#16a34a", height=260)
        else:
            st.markdown('<p style="color:#a3a39b; font-size:0.85rem;">Sin ventas.</p>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Pedidos del día (detalle con filtros) ──────────────────────────────────
    # Para el administrador: TODOS los movimientos del día —incluidos descuentos y
    # cancelados— filtrables por tipo de entrega (mesa / domicilio / para llevar) y por
    # método de pago real (efectivo / transferencia, del libro de pagos).
    st.markdown('<div class="section-title">Pedidos del día</div>', unsafe_allow_html=True)

    metodos_por_pedido = cargar_metodos_por_pedido([p["id"] for p in pedidos])

    f1, f2, f3 = st.columns(3)
    with f1:
        f_tipo = st.selectbox("Tipo", ["Todos", "🪑 Mesa", "🛵 Domicilio", "🛍️ Para llevar"],
                              key="resumen_f_tipo")
    with f2:
        f_metodo = st.selectbox("Método de pago", ["Todos", "💵 Efectivo", "💳 Transferencia"],
                                key="resumen_f_metodo")
    with f3:
        f_estado = st.selectbox("Estado", ["Todos", "Sin cancelados", "Solo cancelados"],
                                key="resumen_f_estado")

    tipo_pick = {"🪑 Mesa": "mesa", "🛵 Domicilio": "domicilio",
                 "🛍️ Para llevar": "para_llevar"}.get(f_tipo)

    filas, filtrados = [], []
    for p in pedidos:
        pid = int(p["id"])
        tn = _tipo_norm(p.get("tipo_entrega"))
        if tipo_pick == "mesa" and tn not in ("mesa", "mesa_qr"):
            continue
        if tipo_pick in ("domicilio", "para_llevar") and tn != tipo_pick:
            continue
        metodos = metodos_por_pedido.get(pid, {})
        if f_metodo == "💵 Efectivo" and metodos.get("efectivo", 0) <= 0:
            continue
        if f_metodo == "💳 Transferencia" and metodos.get("transferencia", 0) <= 0:
            continue
        cancelado = p.get("estado") == "cancelado"
        if f_estado == "Sin cancelados" and cancelado:
            continue
        if f_estado == "Solo cancelados" and not cancelado:
            continue

        dv = int(p.get("descuento_valor") or 0)
        desc_txt = ""
        if dv > 0:
            tipo_d = str(p.get("tipo_descuento") or "").strip()
            desc_txt = f"−${fmt_money(dv)}" + (f" ({tipo_d})" if tipo_d else "")
        motivo = (p.get("motivo_cancelacion") if cancelado else p.get("motivo_descuento")) or ""
        try:
            hora = pd.to_datetime(p.get("fecha")).strftime("%H:%M")
        except Exception:
            hora = ""
        quien = (str(p.get("numero_cliente") or "") if tn in ("mesa", "mesa_qr")
                 else str(p.get("cliente_nombre") or p.get("numero_cliente") or ""))

        filtrados.append(p)
        filas.append({
            "#":            p.get("num_dia") or pid,
            "Hora":         hora,
            "Tipo":         _TIPO_LABEL.get(tn, "🪑 Mesa"),
            "Cliente/Mesa": quien,
            "Items":        formatear_items_texto(p.get("items")),
            "Total":        f"${fmt_money(int(p.get('total') or 0))}",
            "Descuento":    desc_txt,
            "Estado":       "❌ Cancelado" if cancelado else str(p.get("estado") or ""),
            "Pago":         _metodo_label(metodos),
            "Cobrado":      f"${fmt_money(cobrado_pedido(p))}",
            "Saldo":        f"${fmt_money(saldo_pedido(p))}",
            "Mesero":       str(p.get("mesero") or ""),
            "Motivo":       motivo,
        })

    if not filas:
        st.markdown('<p style="color:#a3a39b; font-size:0.85rem; padding:0.5rem 0;">'
                    'No hay pedidos que coincidan con los filtros.</p>', unsafe_allow_html=True)
    else:
        n_f       = len(filtrados)
        n_canc_f  = sum(1 for p in filtrados if p.get("estado") == "cancelado")
        total_f   = sum(int(p.get("total") or 0) for p in filtrados if p.get("estado") != "cancelado")
        cobr_f    = sum(cobrado_pedido(p) for p in filtrados if p.get("estado") != "cancelado")
        desc_f    = sum(int(p.get("descuento_valor") or 0) for p in filtrados)
        st.markdown(
            f'<p style="color:#6b6b64; font-size:0.82rem; margin:2px 0 8px 0;">'
            f'{n_f} pedidos · Facturado ${fmt_money(total_f)} · Cobrado ${fmt_money(cobr_f)} · '
            f'Descuentos ${fmt_money(desc_f)} · Cancelados {n_canc_f}</p>',
            unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(filas), hide_index=True, use_container_width=True)
        st.markdown('<p style="color:#a3a39b; font-size:0.72rem;">El método de pago refleja el '
                    'cobro real registrado; los pedidos sin cobrar (o pagados antes del libro de '
                    'pagos) aparecen como “—”.</p>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Exportar CSV (para el dueño / contabilidad) ────────────────────────────
    # Exporta TODOS los pedidos del día (cobrados, por cobrar y cancelados) con su
    # estado de pago, para que contabilidad tenga el registro completo.
    export = df.copy()
    export["items"] = export["items"].apply(_items_texto)
    # Abonado / Saldo por pedido: el registro de pagos parciales para contabilidad.
    export["abonado"] = df.apply(cobrado_pedido, axis=1)
    export["saldo"]   = df.apply(saldo_pedido, axis=1)
    if "pagado" in export.columns:
        export["pagado"] = export["pagado"].fillna(False).astype(bool).map({True: "Sí", False: "No"})
    export = export.drop(columns=["total_pagado"], errors="ignore")
    export = export.rename(columns={
        "id": "Pedido", "numero_cliente": "Cliente", "items": "Items",
        "total": "Total", "estado": "Estado", "fecha": "Fecha",
        "mesa_id": "Mesa", "motivo_cancelacion": "Motivo cancelación",
        "pagado": "Pagado", "abonado": "Abonado", "saldo": "Saldo",
    })
    csv = export.to_csv(index=False).encode("utf-8-sig")  # BOM → Excel lee acentos
    st.download_button(
        f"⬇ Descargar CSV ({dia.strftime('%d/%m/%Y')})",
        data=csv, file_name=f"ventas_{dia.isoformat()}.csv",
        mime="text/csv", key="resumen_csv",
    )
