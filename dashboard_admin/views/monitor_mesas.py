"""Vista Monitor de mesas: tablero maestro-detalle del salón.

Layout maestro-detalle (master-detail) sin recargar la página:
    - Columna izquierda (25%): lista de mesas como tarjetas-botón, con su estado
      (🟢 libre / 🟠 ocupada / 🔴 atención) y un resumen rápido del pedido activo.
    - Columna derecha (75%): "ambiente de mesa" gobernado por
      st.session_state["mesa_activa"]. Sin mesa → placeholder; con mesa → detalle
      completo de sus pedidos activos + acciones (ticket, cancelar, cobrar).

Reutiliza los helpers del tablero (views/pedidos.py) para no duplicar lógica:
formato de ítems/fechas, badges de estado, tiempo de espera, ticket de cocina e
impresión bajo demanda. El cambio entre la vista general y la de una mesa vive en
session_state, así que el st_autorefresh (rerun parcial) lo conserva.
"""
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from sqlalchemy import text, bindparam
import pandas as pd
import html

from db import engine, fmt_money, cargar_mesas_activas
from views import pedidos


# ── Colores de estado de mesa (paleta Light Mode existente) ─────────────────────
VERDE = "#16a34a"   # libre
AMBAR = "#d97706"   # ocupada (en servicio, sin urgencia)
AZUL  = "#2563eb"   # por cobrar (todo entregado, solo falta el pago)
ROJO  = "#dc2626"   # atención (algo listo por entregar o espera larga)


# ── DB: cobrar (marcar pagado) ──────────────────────────────────────────────────
# 'pagado' es una dimensión aparte del estado de cocina: cobrar NO toca el flujo
# pendiente→…→entregado, solo registra el pago. Una mesa se libera cuando todos sus
# pedidos están pagados (o cancelados), no cuando se entregan.
def cobrar_pedidos(ids):
    """Marca como pagados los pedidos indicados (uno o varios)."""
    ids = [int(i) for i in ids]
    if not ids:
        return
    stmt = text("UPDATE pedidos SET pagado = TRUE WHERE id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    with engine.begin() as conn:
        conn.execute(stmt, {"ids": ids})


# ── Resumen del salón ───────────────────────────────────────────────────────────
def _mesa_id_de_pedido(row, nombre_a_id: dict):
    """Mesa a la que pertenece un pedido: mesa_id real o, en pedidos heredados sin
    id, el nombre guardado en numero_cliente ('Mesa N'). Devuelve int o None."""
    mid = row.get("mesa_id")
    if pd.notna(mid):
        return int(mid)
    cliente = str(row.get("numero_cliente", "") or "").strip().lower()
    return nombre_a_id.get(cliente)


def _estado_mesa(sub: pd.DataFrame):
    """(color, etiqueta) de una mesa según sus pedidos activos (= sin pagar ni
    cancelar). 'sub' ya viene filtrado a esos pedidos."""
    if sub.empty:
        return VERDE, "Libre"
    if (sub["estado"] == "listo").any():
        return ROJO, "Atención · platos listos"
    # Espera larga solo cuenta para pedidos aún en cocina (no entregados).
    en_cocina = sub[sub["estado"].isin(["pendiente", "en preparacion"])]
    if en_cocina.empty:
        # Todo entregado y sin pagar → solo falta cobrar.
        return AZUL, "Por cobrar"
    esperas = [pedidos.minutos_espera(f) for f in en_cocina["fecha"]]
    max_espera = max([m for m in esperas if m is not None], default=0)
    if max_espera >= 10:
        return ROJO, "Atención · espera larga"
    return AMBAR, "Ocupada"


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: MONITOR DE MESAS
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Monitor en vivo: rerun parcial cada 30s; PRESERVA session_state (la mesa
    # seleccionada no se pierde al refrescar). Misma técnica que el tablero.
    st_autorefresh(interval=30000, key="monitor_autorefresh")

    st.markdown('<div class="section-title">🖥️ Monitor de mesas</div>', unsafe_allow_html=True)

    mesas = cargar_mesas_activas()
    if not mesas:
        st.markdown(
            '<p style="color:#9ca3af; font-size:0.85rem;">No hay mesas activas. '
            'Crea mesas en la pestaña 🪑 Mesas.</p>',
            unsafe_allow_html=True,
        )
        return

    nombre_a_id = {str(m["nombre"]).strip().lower(): int(m["id"]) for m in mesas}

    # Lectura en vivo (sin caché, como el tablero) + impresión bajo demanda
    # reutilizando el mismo flujo de un único iframe de views/pedidos.py.
    df = pedidos.cargar_pedidos()
    pedidos._maybe_print_ticket(df)

    # Una mesa está ocupada por sus pedidos sin pagar y sin cancelar (la entrega
    # ya no la libera; el pago sí). 'pagado' puede faltar si el esquema aún no se
    # aplicó: lo tratamos como FALSE.
    pagado = (df["pagado"].fillna(False).astype(bool) if "pagado" in df.columns
              else pd.Series(False, index=df.index))
    activos = df[(df["estado"] != "cancelado") & (~pagado)].copy()
    if not activos.empty:
        activos["__mesa"] = activos.apply(lambda r: _mesa_id_de_pedido(r, nombre_a_id), axis=1)
    else:
        activos["__mesa"] = pd.Series(dtype="object")

    # Subconjunto de pedidos activos por mesa + color/estado de cada una.
    por_mesa, color_por_mesa, estado_por_mesa = {}, {}, {}
    for m in mesas:
        mid = int(m["id"])
        sub = activos[activos["__mesa"] == mid].copy()
        por_mesa[mid] = sub
        color_por_mesa[mid], estado_por_mesa[mid] = _estado_mesa(sub)

    # Saneamos la selección: si la mesa ya no está activa, la limpiamos.
    sel = st.session_state.get("mesa_activa")
    if sel is not None and sel not in por_mesa:
        st.session_state.pop("mesa_activa", None)
        sel = None

    # ── Métricas del salón ──────────────────────────────────────────────────────
    libres    = sum(1 for mid in por_mesa if color_por_mesa[mid] == VERDE)
    ocupadas  = sum(1 for mid in por_mesa if color_por_mesa[mid] == AMBAR)
    por_cobrar = sum(1 for mid in por_mesa if color_por_mesa[mid] == AZUL)
    atencion  = sum(1 for mid in por_mesa if color_por_mesa[mid] == ROJO)
    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.markdown(f'<div class="metric-card"><div class="metric-value">{len(mesas)}</div><div class="metric-label">Mesas</div></div>', unsafe_allow_html=True)
    with m2:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-green">{libres}</div><div class="metric-label">Libres</div></div>', unsafe_allow_html=True)
    with m3:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-accent">{ocupadas}</div><div class="metric-label">Ocupadas</div></div>', unsafe_allow_html=True)
    with m4:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-blue">{por_cobrar}</div><div class="metric-label">Por cobrar</div></div>', unsafe_allow_html=True)
    with m5:
        st.markdown(f'<div class="metric-card"><div class="metric-value" style="color:{ROJO}">{atencion}</div><div class="metric-label">Atención</div></div>', unsafe_allow_html=True)

    # ── CSS: botones-tarjeta del panel izquierdo ────────────────────────────────
    st.markdown("""
    <style>
    /* Tarjetas-botón de mesa (panel maestro). Sobrescriben el botón base. */
    [class*="st-key-mesabtn_"] button {
        text-align: left !important; justify-content: flex-start !important;
        padding: 12px 14px !important; min-height: 62px !important;
        border-radius: 12px !important; border: 1px solid #e5e7eb !important;
        border-left: 4px solid #d1d5db !important; background: #ffffff !important;
        color: #1a1a1a !important; font-size: 0.86rem !important; font-weight: 600 !important;
        box-shadow: 0 1px 2px rgba(0,0,0,0.04) !important; margin: 0 0 8px 0 !important;
        line-height: 1.35 !important; white-space: normal !important;
    }
    [class*="st-key-mesabtn_"] button p { text-align: left !important; }
    [class*="st-key-mesabtn_"] button:hover {
        border-color: #9ca3af !important; background: #f9fafb !important; color: #111827 !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # CSS dinámico: borde por estado + resaltado de la mesa seleccionada.
    dyn = [f".st-key-mesabtn_{mid} button {{ border-left-color: {color} !important; }}"
           for mid, color in color_por_mesa.items()]
    if sel is not None:
        dyn.append(f".st-key-mesabtn_{sel} button {{ background:#1a1a1a !important; color:#ffffff !important; border-color:#1a1a1a !important; }}")
        dyn.append(f".st-key-mesabtn_{sel} button:hover {{ background:#262626 !important; color:#ffffff !important; }}")
        dyn.append(f".st-key-mesabtn_{sel} button p {{ color:#ffffff !important; }}")
    st.markdown(f"<style>{''.join(dyn)}</style>", unsafe_allow_html=True)

    # ── Layout maestro-detalle ──────────────────────────────────────────────────
    col_left, col_right = st.columns([1, 3], gap="large")

    with col_left:
        for m in mesas:
            mid    = int(m["id"])
            nombre = str(m["nombre"])
            sub    = por_mesa[mid]
            color  = color_por_mesa[mid]
            dot    = {VERDE: "🟢", AMBAR: "🟠", AZUL: "🔵", ROJO: "🔴"}[color]

            if sub.empty:
                resumen = "Libre"
            else:
                total = int(sub["total"].sum()) if "total" in sub.columns else 0
                etiqueta = "por cobrar" if color == AZUL else "pedido(s)"
                resumen = f"{len(sub)} {etiqueta} · ${fmt_money(total)}"

            if st.button(f"{dot}  {nombre}\n\n{resumen}", key=f"mesabtn_{mid}",
                         use_container_width=True):
                st.session_state["mesa_activa"] = mid
                st.rerun()

    with col_right:
        if sel is None:
            _placeholder()
        else:
            mesa = next((m for m in mesas if int(m["id"]) == sel), None)
            _detalle_mesa(sel, str(mesa["nombre"]) if mesa else f"Mesa {sel}",
                          por_mesa[sel], color_por_mesa[sel], estado_por_mesa[sel], df)


# ── Detalle: placeholder (sin mesa) ─────────────────────────────────────────────
def _placeholder():
    st.markdown("""
    <div style="border:1px dashed #d1d5db; border-radius:16px; background:#ffffff;
                padding:4rem 2rem; text-align:center; margin-top:0.5rem;
                box-shadow:0 1px 2px rgba(0,0,0,0.04);">
      <div style="font-size:2.6rem; margin-bottom:0.6rem;">🍽️</div>
      <div style="font-family:'Syne',sans-serif; font-size:1.15rem; font-weight:700; color:#1a1a1a;">
        Selecciona una mesa
      </div>
      <div style="font-size:0.85rem; color:#9ca3af; margin-top:6px;">
        Elige una mesa en el panel izquierdo para ver los detalles de su pedido.
      </div>
    </div>
    """, unsafe_allow_html=True)


# ── Detalle: ambiente de una mesa ───────────────────────────────────────────────
def _detalle_mesa(mid: int, nombre: str, sub: pd.DataFrame, color: str,
                  estado_txt: str, df_full: pd.DataFrame):
    total_activo = int(sub["total"].sum()) if not sub.empty and "total" in sub.columns else 0

    # Cobradas hoy (contexto): pedidos pagados de esta mesa con fecha de hoy.
    # (Sin columna de fecha de pago, usamos 'fecha' del pedido, como el resto del
    # panel — misma convención que ventas_hoy en el tablero.)
    try:
        hoy = pd.Timestamp.now().normalize()
        pagado = df_full["pagado"].fillna(False).astype(bool)
        cerr = df_full[
            pagado
            & (df_full["mesa_id"] == mid)
            & (pd.to_datetime(df_full["fecha"], errors="coerce").dt.normalize() == hoy)
        ]
        cerradas_n = len(cerr)
        cerradas_total = int(cerr["total"].sum()) if "total" in cerr.columns else 0
    except Exception:
        cerradas_n, cerradas_total = 0, 0

    # Encabezado del ambiente de mesa.
    st.markdown(f"""
    <div class="order-card" style="border-left:4px solid {color}; margin-bottom:1rem;">
      <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
          <div class="order-id">Ambiente de mesa</div>
          <div style="font-family:'Syne',sans-serif; font-size:1.4rem; font-weight:800; color:#1a1a1a;">🪑 {html.escape(nombre)}</div>
          <div style="font-size:0.8rem; color:{color}; font-weight:600; margin-top:2px;">{html.escape(estado_txt)}</div>
        </div>
        <div style="text-align:right;">
          <div class="metric-label">Total en mesa</div>
          <div class="order-total" style="font-size:1.4rem;">${fmt_money(total_activo)}</div>
          <div style="font-size:0.72rem; color:#9ca3af; margin-top:4px;">Cobradas hoy: {cerradas_n} · ${fmt_money(cerradas_total)}</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Acciones de mesa.
    a1, a2 = st.columns([2, 1])
    with a1:
        if not sub.empty:
            if st.button("💵 Cobrar y cerrar mesa", key=f"mon_cobrar_mesa_{mid}", type="primary"):
                st.session_state["mon_confirm_cobrar_mesa"] = mid
                st.rerun()
    with a2:
        if st.button("✕ Deseleccionar", key=f"mon_deselect_{mid}"):
            st.session_state.pop("mesa_activa", None)
            st.rerun()

    if st.session_state.get("mon_confirm_cobrar_mesa") == mid:
        st.warning(f"¿Cobrar y cerrar **{nombre}**? Sus {len(sub)} pedido(s) se marcarán como pagados y la mesa quedará libre.")
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("Sí, cobrar mesa", key=f"mon_confirm_cobrar_si_{mid}", type="primary"):
                cobrar_pedidos(sub["id"].tolist())
                st.session_state.pop("mon_confirm_cobrar_mesa", None)
                st.rerun()
        with cc2:
            if st.button("Cancelar", key=f"mon_confirm_cobrar_no_{mid}"):
                st.session_state.pop("mon_confirm_cobrar_mesa", None)
                st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    if sub.empty:
        st.markdown(
            '<p style="color:#9ca3af; font-size:0.9rem; padding:1.5rem 0; text-align:center;">'
            'Esta mesa está libre. No tiene pedidos activos.</p>',
            unsafe_allow_html=True,
        )
        return

    # Tarjetas de pedido + acciones por pedido.
    sub = sub.sort_values("fecha") if "fecha" in sub.columns else sub
    for idx, (_, row) in enumerate(sub.iterrows()):
        _detalle_pedido(row, idx)


def _detalle_pedido(row, idx: int):
    pid     = int(row["id"])
    estado  = row.get("estado", "pendiente")
    items   = pedidos.formatear_items(row.get("items", []))
    total_p = row.get("total", 0)
    fecha   = pedidos.formatear_fecha(row.get("fecha"))
    uid     = f"mon_{pid}_{idx}"

    mins      = pedidos.minutos_espera(row.get("fecha"))
    color_urg = pedidos.urgencia(mins, estado)
    chip = (f'<div style="font-size:0.72rem; color:{color_urg}; font-weight:700; margin-top:6px;">⏱ {mins} min</div>'
            if color_urg else "")
    borde = f' style="border-left:4px solid {color_urg};"' if color_urg else ""

    st.markdown(f"""
    <div class="order-card"{borde}>
      <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
          <div class="order-id">Pedido #{pid}</div>
          <div class="order-items">{items}</div>
          <div class="order-fecha">{fecha}</div>
        </div>
        <div style="text-align:right;">
          {pedidos.badge_html(estado)}
          <div class="order-total" style="margin-top:8px;">${fmt_money(total_p)}</div>
          {chip}
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    b1, b2, b3, b4 = st.columns(4)
    with b1:
        btn_label = pedidos.ESTADO_LABEL_BTN.get(estado)
        if btn_label and st.button(btn_label, key=f"avanzar_{uid}", type="primary"):
            pedidos.avanzar_estado(pid, estado)  # hace st.rerun()
    with b2:
        if st.button("🖨 Ticket", key=f"ticket_{uid}"):
            st.session_state["print_ticket_id"] = pid
            st.rerun()
    with b3:
        if st.button("💵 Cobrar", key=f"cobrar_{uid}", help="Marcar este pedido como pagado"):
            cobrar_pedidos([pid])
            st.rerun()
    with b4:
        if st.button("✕ Cancelar", key=f"cancelar_{uid}"):
            st.session_state["mon_confirmar_cancel"] = pid
            st.rerun()

    # Confirmación de cancelación (ancho completo, debajo de la tarjeta).
    if st.session_state.get("mon_confirmar_cancel") == pid:
        st.warning(f"¿Cancelar el pedido #{pid}?")
        motivo = st.text_input("Motivo (opcional)", key=f"motivo_{uid}",
                               placeholder="Ej: cliente se retiró, error de cocina…")
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("Sí, cancelar", key=f"confirm_cancel_{uid}", type="primary"):
                st.session_state.pop("mon_confirmar_cancel", None)
                pedidos.cancelar_pedido(pid, motivo)  # hace st.rerun()
        with cc2:
            if st.button("Volver", key=f"volver_cancel_{uid}"):
                st.session_state.pop("mon_confirmar_cancel", None)
                st.rerun()
