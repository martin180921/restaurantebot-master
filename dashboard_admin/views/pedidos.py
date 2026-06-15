"""Vista de Pedidos: tablero de estados, alertas de audio y tickets de cocina."""
import streamlit as st
import streamlit.components.v1
from sqlalchemy import text, bindparam
import pandas as pd
import json
import html
from datetime import datetime

from db import (engine, fmt_money, fecha_corta, flash, drain_toasts,
                saldo_pedido, cobrado_pedido)

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


# F1: el esquema (columna motivo_cancelacion) lo garantiza db._ensure_schema()
# al importar db.py, así que aquí ya está disponible.
ESTADOS_ACTIVOS = ["pendiente", "en preparacion", "listo"]


# ── Toasts no bloqueantes (Fase 1) ──────────────────────────────────────────────
# flash()/drain_toasts() viven en db.py (compartidos por todas las vistas). Se
# reexportan aquí para no romper las llamadas existentes (pedidos.flash /
# pedidos.drain_toasts) desde panel.py y monitor_mesas.py.


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
    flash(f"Pedido #{pedido_id} → {siguiente}", "✅")
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
    flash(f"Pedido #{pedido_id} → {anterior}", "↩️")
    st.rerun()

def cancelar_pedido(pedido_id: int, motivo: str = ""):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pedidos SET estado = 'cancelado', motivo_cancelacion = :m WHERE id = :id"),
            {"m": (motivo or "").strip() or None, "id": pedido_id}
        )
    flash(f"Pedido #{pedido_id} cancelado", "🗑️")
    st.rerun()


# ── DB: cobro (pagos completos y parciales) ─────────────────────────────────────
# 'pagado' es una dimensión aparte del estado de cocina: cobrar NO toca el flujo
# pendiente→…→entregado, solo registra el pago. Una mesa se libera cuando todos sus
# pedidos están pagados (o cancelados), no cuando se entregan.
def _distribuir_abono(pendientes, monto):
    """Reparte 'monto' entre 'pendientes' (del más antiguo al más nuevo) sin pasarse
    del saldo de cada pedido. Función PURA (sin BD) para poder testearla.

    'pendientes' = lista de dicts {id, total, total_pagado}. Devuelve la lista de
    actualizaciones [{id, total_pagado, pagado, aplicado}] solo para los pedidos
    tocados; 'aplicado' = lo abonado a ese pedido ahora (una fila del libro 'pagos').
    """
    restante = max(0, int(monto))
    updates = []
    for p in pendientes:
        if restante <= 0:
            break
        total = int(p["total"])
        ya = int(p.get("total_pagado") or 0)
        saldo = max(0, total - ya)
        if saldo <= 0:
            continue
        aplicar = min(saldo, restante)
        nuevo = ya + aplicar
        updates.append({"id": int(p["id"]), "total_pagado": nuevo,
                        "pagado": nuevo >= total, "aplicado": aplicar})
        restante -= aplicar
    return updates


def registrar_pago(ids, monto, metodo="efectivo"):
    """Registra un abono de 'monto' (en 'metodo') contra los pedidos 'ids' (uno o
    varios), repartiéndolo del más antiguo al más nuevo. Cada pedido cuyo saldo quede
    en 0 se marca pagado=TRUE; el resto acumula en total_pagado y sigue pagado=FALSE.
    Cada abono aplicado se anota además en el libro 'pagos' (método + hora real de
    pago). Lee y escribe en la MISMA transacción (FOR UPDATE) sobre el saldo real.
    """
    ids = [int(i) for i in ids]
    monto = int(round(float(monto or 0)))
    metodo = metodo if metodo in ("efectivo", "transferencia") else "efectivo"
    if not ids or monto <= 0:
        return
    sel = text("""
        SELECT id, total, COALESCE(total_pagado, 0) AS total_pagado
        FROM pedidos
        WHERE id IN :ids AND pagado = FALSE AND estado <> 'cancelado'
        ORDER BY fecha, id
        FOR UPDATE
    """).bindparams(bindparam("ids", expanding=True))
    upd = text("UPDATE pedidos SET total_pagado = :total_pagado, pagado = :pagado WHERE id = :id")
    ins = text("INSERT INTO pagos (pedido_id, monto, metodo) VALUES (:pedido_id, :monto, :metodo)")
    with engine.begin() as conn:
        pendientes = [dict(r) for r in conn.execute(sel, {"ids": ids}).mappings().all()]
        for u in _distribuir_abono(pendientes, monto):
            conn.execute(upd, {"total_pagado": u["total_pagado"], "pagado": u["pagado"], "id": u["id"]})
            conn.execute(ins, {"pedido_id": u["id"], "monto": u["aplicado"], "metodo": metodo})


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
        return fecha_corta(pd.to_datetime(fecha))  # U4: portable, en español
    except Exception:
        return str(fecha)


# ── Tiempo de espera (U2) ───────────────────────────────────────────────────────
def minutos_espera(fecha):
    """Minutos desde 'fecha', o None si no se puede calcular."""
    if pd.isna(fecha):
        return None
    try:
        dt = pd.to_datetime(fecha).to_pydatetime()
        return max(0, int((datetime.now() - dt).total_seconds() // 60))
    except Exception:
        return None

def urgencia(mins, estado):
    """Color de antigüedad para pedidos ACTIVOS: verde <5 min, ámbar 5-10, rojo
    >10. Devuelve un hex o None (sin acento para entregados/cancelados)."""
    if estado not in ("pendiente", "en preparacion") or mins is None:
        return None
    if mins >= 10:
        return "#dc2626"  # rojo: urgente
    if mins >= 5:
        return "#d97706"  # ámbar: atención
    return "#16a34a"      # verde: reciente


# ── Origen del pedido (U6) ──────────────────────────────────────────────────────
def icono_cliente(row, mesa_nombres=None):
    """(emoji, etiqueta) según el origen: 🪑 mesa (en local) o 📱 teléfono."""
    mid = row.get("mesa_id")
    cliente = str(row.get("numero_cliente", "—") or "—")
    if mid is not None and not pd.isna(mid):
        nombre = (mesa_nombres or {}).get(int(mid))
        return ("🪑", nombre or cliente)
    if cliente.lower().startswith("mesa"):
        return ("🪑", cliente)
    return ("📱", cliente)


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
    fecha_str  = html.escape(str(fecha)) if fecha else fecha_corta(datetime.now())
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


# ── Impresión bajo demanda (P3) ─────────────────────────────────────────────────
def _emit_print(ticket_html: str, tid: int) -> None:
    """Emite UN solo iframe de impresión (antes había uno por tarjeta → decenas
    de iframes en un tablero ocupado). Intenta abrir la ventana automáticamente;
    si el navegador bloquea los popups, deja un botón para imprimir con un clic.
    """
    escaped = (ticket_html.replace("\\", "\\\\").replace("`", "\\`")
               .replace("${", "\\${").replace("</script>", "<" + "/script>"))
    fn = f"imprimir_{tid}"
    st.components.v1.html(f"""
    <div style="font-family:'DM Sans',sans-serif; font-size:0.8rem; color:#6b7280; display:flex; align-items:center; gap:10px;">
      <span id="msg_{tid}">🖨 Abriendo impresión del ticket #{tid}…</span>
      <button onclick="{fn}()" style="padding:6px 14px; background:#1a1a1a; color:#fff; border:none; border-radius:8px; font-family:'DM Sans',sans-serif; font-size:0.78rem; cursor:pointer;">Imprimir #{tid}</button>
    </div>
    <script>
      function {fn}() {{
        var w = window.open('', '_blank', 'width=320,height=500,scrollbars=yes');
        if (!w) {{
          document.getElementById('msg_{tid}').textContent =
            'Permite las ventanas emergentes y toca Imprimir.';
          return;
        }}
        w.document.write(`{escaped}`); w.document.close(); w.focus();
        setTimeout(function() {{ w.print(); w.close(); }}, 300);
      }}
      {fn}();  // intento automático (funciona si los popups están permitidos)
    </script>
    """, height=44)


def _maybe_print_ticket(df: pd.DataFrame) -> None:
    """Si una tarjeta pidió imprimir (print_ticket_id), genera y emite el ticket."""
    tid = st.session_state.pop("print_ticket_id", None)
    if tid is None:
        return
    match = df[df["id"] == tid]
    if match.empty:
        return
    r = match.iloc[0]
    ticket_html = generar_ticket_html(
        int(r["id"]), r.get("numero_cliente", "—"), r.get("items", []),
        r.get("total", 0), formatear_fecha(r.get("fecha")), r.get("estado", "pendiente"),
    )
    _emit_print(ticket_html, int(tid))


# ── Modal de cancelación (Fase 3) ───────────────────────────────────────────────
# @st.dialog abre un pop-up centrado en lugar del aviso inline que empujaba el
# layout. Se comparte entre el tablero y el monitor de mesas (claves por 'uid').
@st.dialog("Cancelar pedido")
def dialog_cancelar(pid: int, uid: str):
    pid = int(pid)
    st.markdown(
        f"¿Seguro que quieres **cancelar el pedido #{pid}**?  \n"
        "Esta acción no se puede deshacer."
    )
    motivo = st.text_input(
        "Motivo (opcional)", key=f"motivo_{uid}",
        placeholder="Ej: cliente se retiró, error de cocina…",
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✕ Sí, cancelar", key=f"confirm_cancel_{uid}", type="primary",
                     use_container_width=True):
            cancelar_pedido(pid, motivo)   # flashea toast + st.rerun()
    with c2:
        if st.button("Volver", key=f"volver_cancel_{uid}", use_container_width=True):
            st.rerun()


# ── Modal de cobro: efectivo/transferencia y abonos parciales (Fase: pagos) ──────
# Pop-up centrado compartido entre el tablero y el monitor de mesas
# (pedidos.dialog_cobrar). 'ids' = pedidos a cobrar (uno o varios); 'titulo' = mesa
# o pedido; 'total' = saldo pendiente total (lo calcula la vista con db.saldo_pedido).
@st.dialog("💵 Cobrar")
def dialog_cobrar(ids, titulo, total, uid):
    ids = [int(i) for i in ids]
    total = max(0, int(total))

    st.markdown(f"**{html.escape(str(titulo))}**")
    st.markdown(
        '<div style="font-size:0.78rem; color:#6b7280; text-transform:uppercase; '
        'letter-spacing:0.04em;">Total por pagar</div>'
        f'<div style="font-family:\'Syne\',sans-serif; font-size:1.9rem; font-weight:800; '
        f'color:#1a1a1a; line-height:1.1;">${fmt_money(total)}</div>',
        unsafe_allow_html=True,
    )

    if total <= 0:
        st.info("Esta cuenta ya está saldada.")
        if st.button("Cerrar", key=f"volver_cobrar_{uid}", use_container_width=True):
            st.rerun()
        return

    st.markdown("<br>", unsafe_allow_html=True)

    # Método de pago. El CSS global pinta el st.radio horizontal como píldoras
    # (segmented-control) y oculta su label → ponemos uno propio con st.caption.
    st.caption("Método de pago")
    metodo = st.radio(
        "Método de pago", ["💵 Efectivo", "💳 Transferencia"],
        horizontal=True, label_visibility="collapsed", key=f"metodo_{uid}",
    )
    es_efectivo = metodo == "💵 Efectivo"

    # Transferencia: el tender de efectivo NO aplica. Limpiamos su estado para que un
    # valor "insuficiente" previo no quede bloqueando el cobro ni reaparezca al volver
    # a Efectivo; y la validación de abajo ignora el cálculo de cambio (solo exige
    # abono > 0). Seguro: el widget 'recibe' solo se instancia en la rama de efectivo,
    # así que aquí (transferencia) podemos limpiar su clave sin chocar con el widget.
    if not es_efectivo:
        st.session_state.pop(f"recibe_{uid}", None)

    # Monto a abonar: por defecto el total (cobro completo). Reducirlo = abono
    # parcial. min_value=0 bloquea negativos; max_value=total impide sobre-cobrar.
    abono = int(st.number_input(
        "Monto a abonar", min_value=0, max_value=total, value=total, step=1000,
        format="%d", key=f"abono_{uid}",
        help="Por defecto cobra el total. Reduce el monto para registrar un abono parcial.",
    ) or 0)

    # Efectivo: cuánto entrega el cliente → cambio = entregado − abono (nunca < 0).
    efectivo_corto = False
    if es_efectivo:
        recibe = int(st.number_input(
            "¿Con cuánto paga el cliente?", min_value=0, value=abono, step=1000,
            format="%d", key=f"recibe_{uid}",
        ) or 0)
        if recibe >= abono:
            st.markdown(
                '<div style="background:#dcfce7; border:1px solid #86efac; border-radius:10px; '
                'padding:10px 14px; margin-top:6px; display:flex; justify-content:space-between; '
                'align-items:center;"><span style="color:#14532d; font-weight:600;">Cambio</span>'
                f'<span style="font-family:\'Syne\',sans-serif; font-weight:800; font-size:1.2rem; '
                f'color:#14532d;">${fmt_money(recibe - abono)}</span></div>',
                unsafe_allow_html=True,
            )
        else:
            # No mostramos cambio negativo: el efectivo no alcanza para el abono.
            efectivo_corto = True
            st.markdown(
                '<div style="background:#fef3c7; border:1px solid #fcd34d; border-radius:10px; '
                'padding:10px 14px; margin-top:6px; color:#92400e; font-weight:600;">'
                f'Efectivo insuficiente: faltan ${fmt_money(abono - recibe)} para abonar '
                f'${fmt_money(abono)}.</div>',
                unsafe_allow_html=True,
            )

    if abono < total and not efectivo_corto:
        st.markdown(
            f'<div style="font-size:0.8rem; color:#1e3a8a; margin-top:8px;">Abono parcial · '
            f'saldo restante: <b>${fmt_money(total - abono)}</b> (la cuenta sigue abierta).</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        # Transferencia se confirma solo con abono > 0 (sin cálculo de cambio); en
        # efectivo además exige que el tender alcance (no efectivo_corto).
        if st.button("✓ Confirmar pago", key=f"confirm_cobrar_{uid}", type="primary",
                     use_container_width=True,
                     disabled=(abono <= 0 or (es_efectivo and efectivo_corto))):
            registrar_pago(ids, abono, "efectivo" if es_efectivo else "transferencia")
            if abono >= total:
                flash(f"Pago completo · {titulo} · ${fmt_money(total)}", "💵")
            else:
                flash(f"Abono registrado · {titulo} · saldo ${fmt_money(total - abono)}", "🧾")
            st.rerun()
    with c2:
        if st.button("Cancelar", key=f"volver_cobrar_{uid}", use_container_width=True):
            st.rerun()


def render_pedidos(dataframe: pd.DataFrame, tab_key: str = "all", mesa_nombres=None):
    if dataframe.empty:
        st.markdown('<p style="color:#9ca3af; font-size:0.85rem; padding:1rem 0;">Sin pedidos en esta categoría.</p>', unsafe_allow_html=True)
        return
    for idx, (_, row) in enumerate(dataframe.iterrows()):
        pid     = row["id"]
        estado  = row.get("estado", "pendiente")
        emoji, etiqueta = icono_cliente(row, mesa_nombres)   # U6
        items   = formatear_items(row.get("items", []))
        total_p = row.get("total", 0)
        fecha   = formatear_fecha(row.get("fecha"))
        uid     = f"{tab_key}_{pid}_{idx}"

        # U2: acento de urgencia por tiempo de espera (solo pedidos activos)
        mins        = minutos_espera(row.get("fecha"))
        color_urg   = urgencia(mins, estado)
        borde       = f' style="border-left:4px solid {color_urg};"' if color_urg else ""
        chip_espera = (f'<div style="font-size:0.72rem; color:{color_urg}; font-weight:700; '
                       f'margin-top:6px; white-space:nowrap;">⏱ {mins} min</div>') if color_urg else ""

        # Fix 2: info + actions. Wider actions col ([3,2]) so en el panel lateral
        # angosto los botones no se desbordan de la tarjeta.
        col_info, col_acciones = st.columns([3, 2])
        with col_info:
            st.markdown(f"""
            <div class="order-card"{borde}>
              <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <div>
                  <div class="order-id">Pedido #{pid}</div>
                  <div class="order-num">{emoji} {html.escape(str(etiqueta))}</div>
                  <div class="order-items">{items}</div>
                  <div class="order-fecha">{fecha}</div>
                </div>
                <div style="text-align:right;">{badge_html(estado)}<div class="order-total" style="margin-top:8px;">${fmt_money(total_p)}</div>{chip_espera}</div>
              </div>
            </div>
            """, unsafe_allow_html=True)
        with col_acciones:
            # Fix 2: stack buttons vertically, full width, no height spacer
            btn_label = ESTADO_LABEL_BTN.get(estado)
            if btn_label:
                if st.button(btn_label, key=f"avanzar_{uid}", type="primary",
                             use_container_width=True):
                    avanzar_estado(pid, estado)

            # Cobrar: abre el modal de pago (efectivo/transferencia + abonos
            # parciales). Visible mientras el pedido tenga saldo y no esté cancelado;
            # cubre también pedidos de WhatsApp/teléfono (sin mesa), que solo se
            # cobran desde aquí (el monitor solo lista pedidos de mesa).
            saldo = saldo_pedido(row)
            if estado != "cancelado" and saldo > 0:
                if st.button("💵 Cobrar", key=f"cobrar_{uid}", use_container_width=True):
                    dialog_cobrar([int(pid)], f"Pedido #{int(pid)}", saldo, uid)

            # P3: botón nativo; la impresión usa UN solo iframe bajo demanda
            # (_maybe_print_ticket en render()), no un iframe por tarjeta.
            if st.button("🖨 Ticket", key=f"ticket_{uid}", use_container_width=True):
                st.session_state["print_ticket_id"] = int(pid)
                st.rerun()

            if estado in ESTADOS and ESTADOS.index(estado) > 0 and estado != "entregado":
                if st.button("↩ Revertir", key=f"revertir_{uid}", use_container_width=True):
                    revertir_estado(pid, estado)
            # F1: cancelar disponible para cualquier pedido activo (antes solo pendiente)
            # Fase 3: abre un modal centrado en vez de un aviso inline.
            if estado in ESTADOS_ACTIVOS:
                if st.button("✕ Cancelar", key=f"cancelar_{uid}", use_container_width=True):
                    dialog_cancelar(pid, uid)


# ── Agrupación por mesa (Req 3) ────────────────────────────────────────────────
@st.cache_data(ttl=60)  # P1: el mapa de mesas cambia poco; TTL corto basta
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

def _con_saldo_mask(df: pd.DataFrame) -> pd.Series:
    """Serie booleana de pedidos con SALDO PENDIENTE (> 0): excluye los pagados
    (pagado=TRUE) y los de saldo cero (total − total_pagado <= 0). Equivale a
    saldo_pedido(row) > 0 pero vectorizado. Defensiva ante columnas faltantes
    pre-migración (se tratan como FALSE / 0).

    Es la definición de 'activo' que comparten el tablero por estado, sus cuentas y
    la vista por mesa: un pedido cobrado (completo o de saldo cero) NO debe seguir en
    el tablero activo, igual que en el Monitor (cobrar = pedido resuelto)."""
    idx = df.index
    pagado = (df["pagado"].fillna(False).astype(bool) if "pagado" in df.columns
              else pd.Series(False, index=idx))
    total = (pd.to_numeric(df["total"], errors="coerce").fillna(0)
             if "total" in df.columns else pd.Series(0, index=idx))
    abonado = (pd.to_numeric(df["total_pagado"], errors="coerce").fillna(0)
               if "total_pagado" in df.columns else pd.Series(0, index=idx))
    return (~pagado) & ((total - abonado) > 0)


def render_por_mesa(df: pd.DataFrame, mesa_nombres: dict):
    """Agrupa y renderiza los pedidos ACTIVOS por mesa.

    'Activo' aquí = en cocina (pendiente/en preparación/listo) y CON saldo pendiente,
    igual que en el Monitor: cobrar libera la mesa, así que un pedido ya saldado no
    debe seguir apareciendo aunque su estado de cocina no se haya avanzado a
    'entregado'. Ver _con_saldo_mask (excluye pagado=TRUE o saldo <= 0)."""
    activos = df[df["estado"].isin(ESTADOS_ACTIVOS) & _con_saldo_mask(df)].copy()
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
        render_pedidos(sub, tab_key=f"mesa{gi}", mesa_nombres=mesa_nombres)


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: PEDIDOS
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Refresco en vivo: SOLO este fragmento se re-ejecuta en el intervalo (no toda
    # la app ni panel.py), a diferencia del antiguo st_autorefresh → menos parpadeo
    # y menos carga. Las acciones (avanzar/cobrar/cancelar) siguen llamando st.rerun()
    # (scope app) para refrescar todo y vaciar los toasts en panel.py.
    _tablero_en_vivo()


@st.fragment(run_every="30s")
def _tablero_en_vivo():
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
        # U5: campana sintetizada con Web Audio — sin depender de URLs externas
        # (antes soundjay/freesound podían dar 404/CORS y dejar la cocina sin aviso).
        st.components.v1.html("""
        <script>
        (function(){
          try {
            var AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return;
            var ctx = new AC();
            function tono(freq, inicio, dur){
              var o = ctx.createOscillator(), g = ctx.createGain();
              o.connect(g); g.connect(ctx.destination);
              o.type = 'sine'; o.frequency.value = freq;
              var t = ctx.currentTime + inicio;
              g.gain.setValueAtTime(0.0001, t);
              g.gain.exponentialRampToValueAtTime(0.5, t + 0.03);
              g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
              o.start(t); o.stop(t + dur + 0.02);
            }
            var sonar = function(){ tono(880, 0, 0.25); tono(1175, 0.18, 0.38); };
            if (ctx.state === 'suspended') { ctx.resume().then(sonar).catch(function(){}); }
            else { sonar(); }
          } catch(e) {}
        })();
        </script>
        """, height=0)

    # 'activo' = pedido con saldo pendiente (no pagado y total − total_pagado > 0),
    # la misma definición que usa el Monitor: cobrar = pedido resuelto. Las cuentas y
    # pestañas de cocina (pendientes/en prep/listos) excluyen los ya saldados; 'Todos'
    # los conserva como catálogo. Ver _con_saldo_mask.
    activo = _con_saldo_mask(df)

    total      = len(df)
    pend       = len(df[(df["estado"] == "pendiente")      & activo])
    en_prep    = len(df[(df["estado"] == "en preparacion") & activo])
    listos     = len(df[(df["estado"] == "listo")          & activo])
    entregados = len(df[df["estado"] == "entregado"])
    cancelados = len(df[df["estado"] == "cancelado"]) if "cancelado" in df["estado"].values else 0
    # Ventas = dinero realmente cobrado, no solo entregado. Suma lo COBRADO de cada
    # pedido no cancelado de hoy (cobrado_pedido = total si pagado, si no el abono
    # parcial total_pagado) → incluye las cuentas parciales abiertas. Defensivo ante
    # columnas faltantes pre-migración (cobrado_pedido las trata como 0/FALSE).
    hoy_df = df[
        (pd.to_datetime(df["fecha"]).dt.date == datetime.now().date()) &
        (df["estado"] != "cancelado")
    ]
    ventas_hoy = int(hoy_df.apply(cobrado_pedido, axis=1).sum()) if not hoy_df.empty else 0

    # Tira compacta de stats: el tablero vive ahora en un panel lateral angosto, así
    # que las 5 metric-cards grandes partían el texto en vertical ("PEN/DIE/NTES").
    # Una tira flex con números pequeños cabe sin romperse y prioriza la cola de
    # cocina (pendientes · en prep · listos) + lo cobrado hoy. 'Total pedidos' se
    # quitó por ser el dato menos accionable en vivo (sigue en la pestaña "Todos").
    st.markdown(
        '<div class="ped-stats">'
        f'<div class="ped-stat"><span class="ped-stat-n metric-accent">{pend}</span>'
        '<span class="ped-stat-l">Pendientes</span></div>'
        f'<div class="ped-stat"><span class="ped-stat-n metric-blue">{en_prep}</span>'
        '<span class="ped-stat-l">En prep.</span></div>'
        f'<div class="ped-stat"><span class="ped-stat-n metric-green">{listos}</span>'
        '<span class="ped-stat-l">Listos</span></div>'
        f'<div class="ped-stat"><span class="ped-stat-n">${fmt_money(ventas_hoy)}</span>'
        '<span class="ped-stat-l">Ventas hoy</span></div>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)

    # P3: emite el ticket pedido por una tarjeta (un único iframe, no uno por orden).
    _maybe_print_ticket(df)

    # Req 3: alterna entre el tablero agrupado por mesa y la vista por estado.
    vista = st.radio(
        "Vista", ["🪑 Por mesa", "📋 Por estado"],
        horizontal=True, label_visibility="collapsed", key="vista_pedidos"
    )
    st.markdown("<br>", unsafe_allow_html=True)

    mesa_nombres = cargar_mesas_nombres()  # U6/P1: una sola lectura cacheada

    if vista == "🪑 Por mesa":
        render_por_mesa(df, mesa_nombres)
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
            render_pedidos(df, "todos", mesa_nombres=mesa_nombres)
        with tab_pend:
            render_pedidos(df[(df["estado"] == "pendiente") & activo].copy(), "pend", mesa_nombres=mesa_nombres)
        with tab_prep:
            render_pedidos(df[(df["estado"] == "en preparacion") & activo].copy(), "prep", mesa_nombres=mesa_nombres)
        with tab_listo:
            render_pedidos(df[(df["estado"] == "listo") & activo].copy(), "listo", mesa_nombres=mesa_nombres)
        with tab_entregado:
            render_pedidos(df[df["estado"] == "entregado"].copy(), "entregado", mesa_nombres=mesa_nombres)
        with tab_cancelado:
            render_pedidos(df[df["estado"] == "cancelado"].copy(), "cancelado", mesa_nombres=mesa_nombres)

    st.markdown("<br>", unsafe_allow_html=True)
    col_r1, col_r2 = st.columns([1, 4])
    with col_r1:
        if st.button("🔄 Actualizar ahora"):
            st.rerun()
    with col_r2:
        st.markdown('<p style="color:#9ca3af; font-size:0.75rem; padding-top:8px;">Los cambios se guardan inmediatamente en la base de datos.</p>', unsafe_allow_html=True)
