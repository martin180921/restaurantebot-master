"""Vista de Pedidos: tablero de estados, alertas de audio y tickets de cocina."""
import streamlit as st
import streamlit.components.v1
from sqlalchemy import text
import pandas as pd
import json
import html
from datetime import datetime

from db import engine, fmt_money

# ── Constantes ─────────────────────────────────────────────────────────────────
ESTADOS = ["pendiente", "en preparacion", "listo", "entregado"]
ESTADO_SIGUIENTE = {
    "pendiente":      "en preparacion",
    "en preparacion": "listo",
    "listo":          "entregado",
    "entregado":      None
}
ESTADO_LABEL_BTN = {
    "pendiente":      "▶ Iniciar preparación",
    "en preparacion": "✓ Marcar listo",
    "listo":          "✓ Entregar",
    "entregado":      None
}


# ── DB: pedidos ────────────────────────────────────────────────────────────────
def cargar_pedidos():
    with engine.connect() as conn:
        resultado = conn.execute(text("SELECT * FROM pedidos ORDER BY fecha DESC"))
        return pd.DataFrame(resultado.fetchall(), columns=resultado.keys())

def avanzar_estado(pedido_id: int, estado_actual: str):
    siguiente = ESTADO_SIGUIENTE.get(estado_actual)
    if not siguiente:
        return
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pedidos SET estado = :estado WHERE id = :id"),
            {"estado": siguiente, "id": pedido_id}
        )
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
    st.rerun()

def cancelar_pedido(pedido_id: int):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pedidos SET estado = :estado WHERE id = :id"),
            {"estado": "cancelado", "id": pedido_id}
        )
    st.rerun()


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
    # C3: en la BD 'items' es TEXT con JSON; hay que parsearlo o el tablero
    # mostraba la cadena cruda [{"nombre":...}] en vez de "Pizza x2".
    if isinstance(items_raw, str):
        try:
            items_raw = json.loads(items_raw)
        except (ValueError, TypeError):
            return html.escape(items_raw)  # C2
    if isinstance(items_raw, list):
        # C2: escapamos nombres (pueden venir de entrada no confiable).
        return ", ".join(
            html.escape(f"{i.get('nombre','?')} x{i.get('cantidad',1)}")
            if isinstance(i, dict) else html.escape(str(i))
            for i in items_raw
        )
    return html.escape(str(items_raw))

def formatear_fecha(fecha) -> str:
    if pd.isna(fecha):
        return "—"
    try:
        return pd.to_datetime(fecha).strftime("%-d %b · %H:%M")
    except:
        return str(fecha)


# ── Ticket de cocina ───────────────────────────────────────────────────────────
def generar_ticket_html(pid, cliente, items_raw, total_p, fecha, estado):
    """Genera el HTML del ticket termico para imprimir."""
    if isinstance(items_raw, str):
        try:
            items_list = json.loads(items_raw)
        except:
            items_list = []
    elif isinstance(items_raw, list):
        items_list = items_raw
    else:
        items_list = []

    lineas_items = ""
    for item in items_list:
        if isinstance(item, dict):
            qty    = html.escape(str(item.get("cantidad", 1)))      # C2
            nombre = html.escape(str(item.get("nombre", "?")))      # C2
            lineas_items += f"<tr><td>{qty}x</td><td>{nombre}</td></tr>"
        else:
            lineas_items += f"<tr><td>1x</td><td>{html.escape(str(item))}</td></tr>"

    # C2: cliente y estado se escapan antes de inyectarse en el ticket.
    cliente    = html.escape(str(cliente))
    fecha_str  = html.escape(str(fecha)) if fecha else datetime.now().strftime("%-d %b · %H:%M")
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
  table{{width:100%;border-collapse:collapse;margin:6px 0;}}
  td{{padding:2px 0;vertical-align:top;}}
  td:first-child{{width:30px;font-weight:bold;}}
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
  <table>{lineas_items}</table>
  <div class="divider"></div>
  <div class="total-row">TOTAL: ${total_fmt}</div>
  <div class="divider"></div>
  <div class="footer">--- Control de Cocina ---</div>
</body></html>"""


def render_pedidos(dataframe: pd.DataFrame, tab_key: str = "all"):
    if dataframe.empty:
        st.markdown('<p style="color:#9ca3af; font-size:0.85rem; padding:1rem 0;">Sin pedidos en esta categoría.</p>', unsafe_allow_html=True)
        return
    for idx, (_, row) in enumerate(dataframe.iterrows()):
        pid     = row["id"]
        estado  = row.get("estado", "pendiente")
        cliente = row.get("numero_cliente", "—")
        items   = formatear_items(row.get("items", []))
        total_p = row.get("total", 0)
        fecha   = formatear_fecha(row.get("fecha"))
        uid     = f"{tab_key}_{pid}_{idx}"

        # Fix 2: info col wider, actions col narrower and self-contained
        col_info, col_acciones = st.columns([4, 1])
        with col_info:
            st.markdown(f"""
            <div class="order-card">
              <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <div>
                  <div class="order-id">Pedido #{pid}</div>
                  <div class="order-num">📱 {html.escape(str(cliente))}</div>
                  <div class="order-items">{items}</div>
                  <div class="order-fecha">{fecha}</div>
                </div>
                <div style="text-align:right;">
                  {badge_html(estado)}
                  <div class="order-total" style="margin-top:8px;">${fmt_money(total_p)}</div>
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)
        with col_acciones:
            # Fix 2: stack buttons vertically, full width, no height spacer
            btn_label = ESTADO_LABEL_BTN.get(estado)
            if btn_label:
                if st.button(btn_label, key=f"avanzar_{uid}", type="primary"):
                    avanzar_estado(pid, estado)

            # Print ticket button
            items_raw = row.get("items", [])
            ticket_html = generar_ticket_html(pid, cliente, items_raw, total_p, fecha, estado)
            ticket_escaped = ticket_html.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${").replace("</script>", "<" + "/script>")
            print_js = f"""
            <script>
            function imprimirTicket_{uid.replace("-","_")}() {{
                var w = window.open('', '_blank', 'width=320,height=500,scrollbars=yes');
                w.document.write(`{ticket_escaped}`);
                w.document.close();
                w.focus();
                setTimeout(function() {{ w.print(); w.close(); }}, 300);
            }}
            </script>
            <button onclick="imprimirTicket_{uid.replace("-","_")}()" style="
                width:100%; margin-top:4px; padding:6px 8px;
                background:#ffffff; color:#374151;
                border:1px solid #d1d5db; border-radius:8px;
                font-family:'DM Sans',sans-serif; font-size:0.75rem;
                cursor:pointer; transition:all 0.15s;
            " onmouseover="this.style.borderColor='#9ca3af';this.style.color='#111827';"
               onmouseout="this.style.borderColor='#d1d5db';this.style.color='#374151';">
                🖨 Ticket
            </button>
            """
            st.components.v1.html(print_js, height=48, scrolling=False)

            if estado in ESTADOS and ESTADOS.index(estado) > 0 and estado != "entregado":
                if st.button("↩ Revertir", key=f"revertir_{uid}"):
                    revertir_estado(pid, estado)
            if estado == "pendiente":
                st.markdown('<div class="btn-cancelar">', unsafe_allow_html=True)
                if st.button("✕ Cancelar", key=f"cancelar_{uid}"):
                    cancelar_pedido(pid)
                st.markdown('</div>', unsafe_allow_html=True)


# ── Agrupación por mesa (Req 3) ────────────────────────────────────────────────
ESTADOS_ACTIVOS = ["pendiente", "en preparacion", "listo"]

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

def render_por_mesa(df: pd.DataFrame, mesa_nombres: dict):
    """Agrupa y renderiza los pedidos ACTIVOS por mesa."""
    activos = df[df["estado"].isin(ESTADOS_ACTIVOS)].copy()
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
        render_pedidos(sub, tab_key=f"mesa{gi}")


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: PEDIDOS
# ══════════════════════════════════════════════════════════════════════════════
def render():
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
        st.components.v1.html("""
        <audio id="alert" preload="auto">
          <source src="https://www.soundjay.com/buttons/sounds/button-09a.mp3" type="audio/mpeg">
          <source src="https://cdn.freesound.org/previews/411/411460_5121236-lq.mp3" type="audio/mpeg">
        </audio>
        <script>
          var a = document.getElementById('alert');
          if (a) {
            var p = a.play();
            if (p !== undefined) { p.catch(function() {}); }
          }
        </script>
        """, height=0)

    total      = len(df)
    pend       = len(df[df["estado"] == "pendiente"])
    en_prep    = len(df[df["estado"] == "en preparacion"])
    listos     = len(df[df["estado"] == "listo"])
    entregados = len(df[df["estado"] == "entregado"])
    cancelados = len(df[df["estado"] == "cancelado"]) if "cancelado" in df["estado"].values else 0
    # Fix: exclude cancelled orders from sales
    ventas_hoy = df[
        (pd.to_datetime(df["fecha"]).dt.date == datetime.now().date()) &
        (df["estado"] != "cancelado")
    ]["total"].sum() if "total" in df.columns else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.markdown(f'<div class="metric-card"><div class="metric-value">{total}</div><div class="metric-label">Total pedidos</div></div>', unsafe_allow_html=True)
    with c2:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-accent">{pend}</div><div class="metric-label">Pendientes</div></div>', unsafe_allow_html=True)
    with c3:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-blue">{en_prep}</div><div class="metric-label">En preparación</div></div>', unsafe_allow_html=True)
    with c4:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-green">{listos}</div><div class="metric-label">Listos</div></div>', unsafe_allow_html=True)
    with c5:
        # Fix 5: reduced clamp max to 1.6rem to handle large numbers
        st.markdown(f'<div class="metric-card"><div class="metric-value" style="font-size:clamp(0.85rem, 1.5vw, 1.6rem); white-space:nowrap;">${fmt_money(ventas_hoy)}</div><div class="metric-label">Ventas hoy</div></div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # Req 3: alterna entre el tablero agrupado por mesa y la vista por estado.
    vista = st.radio(
        "Vista", ["🪑 Por mesa", "📋 Por estado"],
        horizontal=True, label_visibility="collapsed", key="vista_pedidos"
    )
    st.markdown("<br>", unsafe_allow_html=True)

    if vista == "🪑 Por mesa":
        render_por_mesa(df, cargar_mesas_nombres())
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
            render_pedidos(df, "todos")
        with tab_pend:
            render_pedidos(df[df["estado"] == "pendiente"].copy(), "pend")
        with tab_prep:
            render_pedidos(df[df["estado"] == "en preparacion"].copy(), "prep")
        with tab_listo:
            render_pedidos(df[df["estado"] == "listo"].copy(), "listo")
        with tab_entregado:
            render_pedidos(df[df["estado"] == "entregado"].copy(), "entregado")
        with tab_cancelado:
            render_pedidos(df[df["estado"] == "cancelado"].copy(), "cancelado")

    st.markdown("<br>", unsafe_allow_html=True)
    col_r1, col_r2 = st.columns([1, 4])
    with col_r1:
        if st.button("🔄 Actualizar ahora"):
            st.rerun()
    with col_r2:
        st.markdown('<p style="color:#9ca3af; font-size:0.75rem; padding-top:8px;">Los cambios se guardan inmediatamente en la base de datos.</p>', unsafe_allow_html=True)
