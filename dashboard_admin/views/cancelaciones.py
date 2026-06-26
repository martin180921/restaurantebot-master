"""Vista de Cancelaciones (solo administrador): historial de pedidos cancelados.

Vive como pestaña dentro de Caja, junto a Resumen, y por tanto comparte su candado:
solo el rol con capacidad 'see_revenue' (admin) la instancia. Lista los pedidos en
estado 'cancelado' AGRUPADOS POR DÍA (por cancelled_at; los cancelados previos a esa
columna caen bajo su 'fecha' de creación), y de cada uno muestra: hora, pedido,
detalle de ítems, total y el motivo/justificación de la cancelación.
"""
import streamlit as st
from sqlalchemy import text
import pandas as pd
import html

import auth
from db import engine, fmt_money, fecha_larga, titulo_seccion
from views import pedidos


# ── DB ──────────────────────────────────────────────────────────────────────────
def cargar_cancelados(limite: int = 300) -> pd.DataFrame:
    """Pedidos cancelados, del más reciente al más antiguo. 'momento' = cancelled_at
    si existe, si no la fecha de creación (cancelados previos a la columna). Tolerante
    a fallos: DataFrame vacío si la tabla aún no existe."""
    try:
        with engine.connect() as conn:
            res = conn.execute(text("""
                SELECT id, numero_cliente, mesa_id, items, total,
                       motivo_cancelacion,
                       COALESCE(cancelled_at, fecha) AS momento
                FROM pedidos
                WHERE estado = 'cancelado'
                ORDER BY momento DESC
                LIMIT :n
            """), {"n": limite})
            return pd.DataFrame(res.fetchall(), columns=res.keys())
    except Exception:
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: CANCELACIONES (ADMIN)
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Defensa en profundidad: el router solo crea esta pestaña para quien ve ingresos,
    # pero revalidamos por si se alcanza por una ruta inesperada.
    if not auth.can("see_revenue"):
        st.error("🔒 Acceso denegado")
        st.stop()

    st.markdown(titulo_seccion('🚫 Cancelaciones · historial por día'),
                unsafe_allow_html=True)

    df = cargar_cancelados()
    if df.empty:
        st.markdown(
            '<p style="color:#a3a39b; font-size:0.9rem; padding:1.5rem 0; text-align:center;">'
            'No hay pedidos cancelados registrados.</p>',
            unsafe_allow_html=True,
        )
        return

    df["momento"] = pd.to_datetime(df["momento"], errors="coerce")
    df["__dia"] = df["momento"].dt.date

    # Métricas de cabecera: cuántas y por cuánto dinero (lo que NO se vendió).
    total_n = len(df)
    total_monto = int(pd.to_numeric(df["total"], errors="coerce").fillna(0).sum())
    hoy = pd.Timestamp.now().date()
    hoy_n = int((df["__dia"] == hoy).sum())
    m1, m2, m3 = st.columns(3)
    with m1:
        st.markdown(f'<div class="metric-card"><div class="metric-value">{total_n}</div>'
                    '<div class="metric-label">Canceladas (total)</div></div>',
                    unsafe_allow_html=True)
    with m2:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-accent">{hoy_n}</div>'
                    '<div class="metric-label">Canceladas hoy</div></div>',
                    unsafe_allow_html=True)
    with m3:
        st.markdown('<div class="metric-card"><div class="metric-value" style="color:#dc2626; '
                    f'font-size:clamp(0.9rem,1.8vw,2rem); white-space:nowrap;">${fmt_money(total_monto)}</div>'
                    '<div class="metric-label">Monto cancelado</div></div>',
                    unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # Agrupado por día (descendente). Cada día abre en un expander con su total.
    dias = sorted([d for d in df["__dia"].dropna().unique()], reverse=True)
    for i, dia in enumerate(dias):
        sub = df[df["__dia"] == dia]
        dia_ts = pd.Timestamp(dia)
        etiqueta_dia = fecha_larga(dia_ts)
        if dia == hoy:
            etiqueta_dia += " · hoy"
        monto_dia = int(pd.to_numeric(sub["total"], errors="coerce").fillna(0).sum())

        with st.expander(f"📅 {etiqueta_dia}  ·  {len(sub)} cancelada(s)  ·  ${fmt_money(monto_dia)}",
                         expanded=(i == 0)):
            for _, row in sub.iterrows():
                _fila_cancelada(row)


def _fila_cancelada(row):
    pid    = int(row["id"])
    items  = pedidos.formatear_items(row.get("items", []))
    total  = int(row.get("total", 0) or 0)
    cliente = str(row.get("numero_cliente", "—") or "—")
    motivo = str(row.get("motivo_cancelacion") or "").strip()
    hora   = ""
    try:
        hora = pd.to_datetime(row["momento"]).strftime("%H:%M")
    except Exception:
        hora = "—"

    motivo_html = (
        f'<div style="margin-top:6px; background:#fef2f2; border:1px solid #fecaca; '
        f'border-radius:8px; padding:8px 12px; font-size:0.82rem; color:#7f1d1d;">'
        f'<b>Motivo:</b> {html.escape(motivo)}</div>'
        if motivo else
        '<div style="margin-top:6px; font-size:0.8rem; color:#a3a39b; font-style:italic;">'
        'Sin motivo registrado.</div>'
    )

    st.markdown(f"""
    <div class="order-card" style="border-left:4px solid #dc2626;">
      <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
          <div class="order-id">Pedido #{pid} · {html.escape(hora)}</div>
          <div class="order-num">{html.escape(cliente)}</div>
          <div class="order-items">{items}</div>
        </div>
        <div style="text-align:right;">
          <span class="badge badge-cancelado">✕ Cancelado</span>
          <div class="order-total" style="margin-top:8px;">${fmt_money(total)}</div>
        </div>
      </div>
      {motivo_html}
    </div>
    """, unsafe_allow_html=True)
