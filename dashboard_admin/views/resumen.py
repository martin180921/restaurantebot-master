"""Vista de Resumen: cierre de caja y ventas por día (F2)."""
import streamlit as st
from sqlalchemy import text
import pandas as pd
import json
from datetime import date

from db import engine, fmt_money, saldo_pedido, cobrado_pedido


# ── DB ───────────────────────────────────────────────────────────────────────────
def cargar_pedidos_dia(dia: date):
    """Pedidos cuya fecha cae en el día indicado (compara la parte de fecha)."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, numero_cliente, items, total, estado, fecha,
                   mesa_id, motivo_cancelacion, pagado, total_pagado
            FROM pedidos
            WHERE fecha::date = :dia
            ORDER BY fecha
        """), {"dia": dia}).mappings().all()
    return [dict(r) for r in rows]


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
    st.markdown('<div class="section-title">📊 Resumen del día</div>', unsafe_allow_html=True)

    dia = st.date_input("Día", value=date.today(), format="DD/MM/YYYY", key="resumen_dia")

    pedidos = cargar_pedidos_dia(dia)
    if not pedidos:
        st.markdown('<p style="color:#9ca3af; font-size:0.9rem; padding:1rem 0;">No hay pedidos registrados en esta fecha.</p>', unsafe_allow_html=True)
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
            st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">Sin platos vendidos.</p>', unsafe_allow_html=True)

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
            st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">Sin ventas.</p>', unsafe_allow_html=True)

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
