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
session_state, así que el refresco en vivo (st.fragment run_every) lo conserva.
"""
import streamlit as st
import pandas as pd
import html

import auth
from db import fmt_money, cargar_mesas_activas, saldo_pedido
from views import pedidos


# ── Colores de estado de mesa (paleta Light Mode existente) ─────────────────────
VERDE = "#16a34a"   # libre
AMBAR = "#d97706"   # ocupada (en servicio, sin urgencia)
AZUL  = "#2563eb"   # por cobrar (todo entregado, solo falta el pago)
ROJO  = "#dc2626"   # atención (algo listo por entregar o espera larga)

# Fase 3: tinte de fondo COMPLETO por estado para las tarjetas-mesa del panel
# izquierdo (antes solo un punto/borde de color). (fondo, fondo_hover, texto):
# fondos claros + texto oscuro de la misma familia → contraste AA en Light Mode.
CARD_TINT = {
    VERDE: ("#dcfce7", "#bbf7d0", "#14532d"),  # Libre       → verde claro
    AMBAR: ("#ffedd5", "#fed7aa", "#7c2d12"),  # Ocupada     → naranja claro
    AZUL:  ("#dbeafe", "#bfdbfe", "#1e3a8a"),  # Por cobrar  → azul claro
    ROJO:  ("#fee2e2", "#fecaca", "#7f1d1d"),  # Atención    → rojo claro
}


# ── Cobro ───────────────────────────────────────────────────────────────────────
# 'pagado' es una dimensión aparte del estado de cocina: cobrar NO toca el flujo
# pendiente→…→entregado, solo registra el pago. Una mesa se libera cuando todos sus
# pedidos están pagados (o cancelados), no cuando se entregan. El cobro (completo o
# parcial, efectivo/transferencia + cambio) vive en el modal compartido
# pedidos.dialog_cobrar; el saldo pendiente sale de db.saldo_pedido (total − abonos).


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
    # Dos vistas aisladas: el salón (mesas) y los pedidos web (Domicilio / Para
    # Llevar). Los pedidos web no ocupan mesa, así que NUNCA aparecen en el salón;
    # esta pestaña los reúne para preparar y despachar sin mezclarlos con el comedor.
    tab_salon, tab_web = st.tabs(["🪑 Salón", "🛵 Pedidos web"])
    with tab_salon:
        # Monitor en vivo: SOLO este fragmento se re-ejecuta en el intervalo (no toda
        # la app ni panel.py) → menos parpadeo. La mesa seleccionada vive en
        # session_state, así que se conserva entre refrescos; las acciones usan
        # st.rerun() (scope app) para refrescar todo.
        _monitor_en_vivo()
    with tab_web:
        _web_en_vivo()


@st.fragment(run_every="30s")
def _monitor_en_vivo():
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

    # CSS dinámico: fondo COMPLETO por estado + resaltado de la mesa seleccionada.
    # Mismo nivel de especificidad que la regla base (declarado después → gana).
    dyn = []
    for mid, color in color_por_mesa.items():
        bg, bg_h, txt = CARD_TINT[color]
        dyn.append(
            f".st-key-mesabtn_{mid} button {{ background:{bg} !important; "
            f"border-left-color:{color} !important; color:{txt} !important; }}"
            f".st-key-mesabtn_{mid} button p {{ color:{txt} !important; }}"
            f".st-key-mesabtn_{mid} button:hover {{ background:{bg_h} !important; "
            f"border-color:{color} !important; color:{txt} !important; }}"
        )
    if sel is not None:
        # Selección: anillo oscuro + negrita; CONSERVA el tinte de estado y el
        # acento lateral (no se toca background ni border-left-color).
        dyn.append(
            f".st-key-mesabtn_{sel} button {{ box-shadow:0 0 0 2px #111827 !important; "
            f"font-weight:800 !important; }}"
        )
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
                saldo = int(sub.apply(saldo_pedido, axis=1).sum())
                etiqueta = "por cobrar" if color == AZUL else "pedido(s)"
                resumen = f"{len(sub)} {etiqueta} · ${fmt_money(saldo)}"

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
    # Saldo pendiente de la mesa = Σ (total − abonos) de sus pedidos activos.
    saldo_activo = int(sub.apply(saldo_pedido, axis=1).sum()) if not sub.empty else 0

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
          <div class="metric-label">Por cobrar</div>
          <div class="order-total" style="font-size:1.4rem;">${fmt_money(saldo_activo)}</div>
          <div style="font-size:0.72rem; color:#9ca3af; margin-top:4px;">Cobradas hoy: {cerradas_n} · ${fmt_money(cerradas_total)}</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Acciones de mesa. El cobro abre el modal compartido (efectivo/transferencia,
    # cambio y abonos parciales). Cobra contra los ids visibles de 'sub' (así también
    # entran los pedidos heredados con mesa_id NULL); un abono parcial deja la mesa
    # abierta, un pago completo la libera.
    a1, a2 = st.columns([2, 1])
    with a1:
        # Cobrar es capacidad bloqueada para el mesero (monitor de solo visualización).
        if not sub.empty and auth.can("cobrar"):
            if st.button("💵 Cobrar mesa", key=f"mon_cobrar_mesa_{mid}",
                         type="primary", use_container_width=True):
                pedidos.dialog_cobrar(sub["id"].tolist(), nombre, saldo_activo, f"mesa_{mid}")
    with a2:
        if st.button("✕ Deseleccionar", key=f"mon_deselect_{mid}",
                     use_container_width=True):
            st.session_state.pop("mesa_activa", None)
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
    total_p = int(row.get("total", 0) or 0)
    saldo   = saldo_pedido(row)          # lo que falta por cobrar de este pedido
    abonado = max(0, total_p - saldo)    # abono parcial ya recibido (0 si no hay)
    fecha   = pedidos.formatear_fecha(row.get("fecha"))
    uid     = f"mon_{pid}_{idx}"

    mins      = pedidos.minutos_espera(row.get("fecha"))
    color_urg = pedidos.urgencia(mins, estado)
    chip = (f'<div style="font-size:0.72rem; color:{color_urg}; font-weight:700; margin-top:6px;">⏱ {mins} min</div>'
            if color_urg else "")
    borde = f' style="border-left:4px solid {color_urg};"' if color_urg else ""
    # Cuando hay abono parcial, el número grande es el SALDO y se anota lo abonado.
    abono_html = (f'<div style="font-size:0.72rem; color:#1e3a8a; font-weight:600; margin-top:2px;">'
                  f'Abonado ${fmt_money(abonado)} de ${fmt_money(total_p)}</div>'
                  if abonado > 0 else "")

    st.markdown(f"""
    <div class="order-card"{borde}>
      <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
          <div class="order-id">Pedido #{pid}</div>
          <div class="order-items">{items}</div>
          <div class="order-fecha">{fecha}</div>
        </div>
        <div style="text-align:right;">{pedidos.badge_html(estado)}<div class="order-total" style="margin-top:8px;">${fmt_money(saldo)}</div>{abono_html}{chip}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    b1, b2, b3, b4 = st.columns(4)
    with b1:
        btn_label = pedidos.ESTADO_LABEL_BTN.get(estado)
        if btn_label and st.button(btn_label, key=f"avanzar_{uid}", type="primary",
                                   use_container_width=True):
            pedidos.avanzar_estado(pid, estado)  # flashea toast + st.rerun()
    with b2:
        if st.button("🖨 Ticket", key=f"ticket_{uid}", use_container_width=True):
            st.session_state["print_ticket_id"] = pid
            st.rerun()
    with b3:
        if auth.can("cobrar") and st.button(
                "💵 Cobrar", key=f"cobrar_{uid}", use_container_width=True,
                help="Cobrar este pedido (efectivo/transferencia, abono parcial)",
                disabled=saldo <= 0):
            pedidos.dialog_cobrar([pid], f"Pedido #{pid}", saldo, uid)
    with b4:
        # Fase 3: modal centrado en vez de aviso inline (compartido con el tablero).
        if st.button("✕ Cancelar", key=f"cancelar_{uid}", use_container_width=True):
            pedidos.dialog_cancelar(pid, uid)


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: PEDIDOS WEB (Domicilio / Para Llevar) — vista de despacho aislada
# ══════════════════════════════════════════════════════════════════════════════
# Los pedidos de la app pública llevan tipo_entrega = 'domicilio' | 'para_llevar' y
# no tienen mesa, así que no aparecen en el salón. Aquí la cocina los ve juntos con
# todo lo necesario para prepararlos y despacharlos: contacto, dirección (domicilio),
# método de pago + cambio, recargo de envío y las mismas acciones del tablero.
TIPO_BADGE = {
    "domicilio":   ("🛵 Domicilio",   "#dbeafe", "#1e3a8a"),
    "para_llevar": ("🛍️ Para llevar", "#ffedd5", "#7c2d12"),
}


def _txt(valor) -> str:
    """str segura para celdas que pueden venir None/NaN."""
    if valor is None:
        return ""
    try:
        if pd.isna(valor):
            return ""
    except (TypeError, ValueError):
        pass
    return str(valor)


@st.fragment(run_every="30s")
def _web_en_vivo():
    st.markdown('<div class="section-title">🛵 Pedidos web · Domicilio y Para Llevar</div>',
                unsafe_allow_html=True)

    df = pedidos.cargar_pedidos()
    pedidos._maybe_print_ticket(df)  # impresión bajo demanda (un solo iframe)

    if "tipo_entrega" not in df.columns:
        st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">Aún no hay pedidos web.</p>',
                    unsafe_allow_html=True)
        return

    web = df[df["tipo_entrega"].isin(["domicilio", "para_llevar"])].copy()
    activos = (web[web["estado"].isin(pedidos.ESTADOS_ACTIVOS)].copy()
               if not web.empty else web)

    n_act    = len(activos)
    n_dom    = int((activos["tipo_entrega"] == "domicilio").sum()) if not activos.empty else 0
    n_lle    = int((activos["tipo_entrega"] == "para_llevar").sum()) if not activos.empty else 0
    n_listos = int((activos["estado"] == "listo").sum()) if not activos.empty else 0

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.markdown(f'<div class="metric-card"><div class="metric-value">{n_act}</div><div class="metric-label">En curso</div></div>', unsafe_allow_html=True)
    with m2:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-blue">{n_dom}</div><div class="metric-label">Domicilio</div></div>', unsafe_allow_html=True)
    with m3:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-accent">{n_lle}</div><div class="metric-label">Para llevar</div></div>', unsafe_allow_html=True)
    with m4:
        st.markdown(f'<div class="metric-card"><div class="metric-value metric-green">{n_listos}</div><div class="metric-label">Listos</div></div>', unsafe_allow_html=True)

    st.markdown('<p style="color:#9ca3af; font-size:0.78rem; margin-top:6px;">No ocupan mesa. '
                'Prepáralos y despáchalos desde aquí.</p>', unsafe_allow_html=True)

    if activos.empty:
        st.markdown('<p style="color:#9ca3af; font-size:0.9rem; padding:1.5rem 0; text-align:center;">'
                    'No hay pedidos web en curso.</p>', unsafe_allow_html=True)
        return

    activos = activos.sort_values("fecha")  # más antiguo primero (urgencia de despacho)
    for idx, (_, row) in enumerate(activos.iterrows()):
        _web_card(row, idx)


def _web_card(row, idx: int):
    pid      = int(row["id"])
    estado   = row.get("estado", "pendiente")
    tipo     = str(row.get("tipo_entrega") or "")
    etiqueta, bg, fg = TIPO_BADGE.get(tipo, ("Web", "#e5e7eb", "#374151"))
    nombre   = _txt(row.get("cliente_nombre")) or _txt(row.get("numero_cliente")) or "Cliente"
    tel      = _txt(row.get("cliente_telefono"))
    direccion = _txt(row.get("direccion"))
    items    = pedidos.formatear_items(row.get("items", []))
    total    = int(row.get("total", 0) or 0)
    fee      = int(row.get("fee", 0) or 0)
    metodo   = _txt(row.get("metodo_pago"))
    paga_con = int(row.get("paga_con", 0) or 0)
    nota     = _txt(row.get("nota_general"))
    fecha    = pedidos.formatear_fecha(row.get("fecha"))
    saldo    = saldo_pedido(row)
    uid      = f"web_{pid}_{idx}"

    mins      = pedidos.minutos_espera(row.get("fecha"))
    color_urg = pedidos.urgencia(mins, estado)
    chip = (f'<div style="font-size:0.72rem; color:{color_urg}; font-weight:700; margin-top:6px;">⏱ {mins} min</div>'
            if color_urg else "")
    borde = f' style="border-left:4px solid {color_urg};"' if color_urg else ""

    contacto = f'👤 {html.escape(nombre)}'
    if tel:
        contacto += f' · 📞 {html.escape(tel)}'
    dir_html = (f'<div class="order-items">📍 {html.escape(direccion)}</div>'
                if tipo == "domicilio" and direccion else "")
    if metodo == "efectivo":
        cambio = max(0, paga_con - total)
        pago_html = (f'💵 Efectivo · paga con ${fmt_money(paga_con)} · cambio ${fmt_money(cambio)}'
                     if paga_con > 0 else '💵 Efectivo')
    elif metodo == "transferencia":
        pago_html = '💳 Transferencia'
    else:
        pago_html = ''
    nota_html = (f'<div style="font-size:0.76rem; color:#b45309; margin-top:4px;">📝 {html.escape(nota)}</div>'
                 if nota else "")
    fee_html = (f'<div style="font-size:0.72rem; color:#9ca3af;">incl. envío ${fmt_money(fee)}</div>'
                if fee else "")

    st.markdown(f"""
    <div class="order-card"{borde}>
      <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
          <span class="badge" style="background:{bg}; color:{fg}; border:1px solid {bg};">{etiqueta}</span>
          <div class="order-id" style="margin-top:6px;">Pedido #{pid}</div>
          <div class="order-num">{contacto}</div>
          {dir_html}
          <div class="order-items">{items}</div>
          <div style="font-size:0.78rem; color:#6b7280; margin-top:4px;">{pago_html}</div>
          {nota_html}
          <div class="order-fecha">{fecha}</div>
        </div>
        <div style="text-align:right;">
          {pedidos.badge_html(estado)}
          <div class="order-total" style="margin-top:8px;">${fmt_money(total)}</div>
          {fee_html}{chip}
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    b1, b2, b3, b4 = st.columns(4)
    with b1:
        btn_label = pedidos.ESTADO_LABEL_BTN.get(estado)
        if btn_label and st.button(btn_label, key=f"avanzar_{uid}", type="primary",
                                   use_container_width=True):
            pedidos.avanzar_estado(pid, estado)  # flashea toast + st.rerun()
    with b2:
        if st.button("🖨 Ticket", key=f"ticket_{uid}", use_container_width=True):
            st.session_state["print_ticket_id"] = pid
            st.rerun()
    with b3:
        if auth.can("cobrar") and st.button(
                "💵 Cobrar", key=f"cobrar_{uid}", use_container_width=True,
                disabled=saldo <= 0):
            pedidos.dialog_cobrar([pid], f"Pedido #{pid}", saldo, uid)
    with b4:
        if st.button("✕ Cancelar", key=f"cancelar_{uid}", use_container_width=True):
            pedidos.dialog_cancelar(pid, uid)
