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
import time

import auth
from db import fmt_money, cargar_mesas_activas, saldo_pedido, titulo_seccion
from views import pedidos
from views import nuevo_pedido as npos


# ── Colores de estado de mesa (paleta Light Mode existente) ─────────────────────
VERDE = "#16a34a"   # libre
AMBAR = "#d97706"   # ocupada (en servicio, sin urgencia)
AZUL  = "#6c5ce0"   # por cobrar (todo entregado, solo falta el pago)
ROJO  = "#dc2626"   # atención (algo listo por entregar o espera larga)

# Fase 3: tinte de fondo COMPLETO por estado para las tarjetas-mesa del panel
# izquierdo (antes solo un punto/borde de color). (fondo, fondo_hover, texto):
# fondos claros + texto oscuro de la misma familia → contraste AA en Light Mode.
CARD_TINT = {
    VERDE: ("#dcfce7", "#bbf7d0", "#14532d"),  # Libre       → verde claro
    AMBAR: ("#ffedd5", "#fed7aa", "#7c2d12"),  # Ocupada     → naranja claro
    AZUL:  ("#e9e7fb", "#d9d4f7", "#4b43b0"),  # Por cobrar  → azul claro
    ROJO:  ("#fee2e2", "#fecaca", "#7f1d1d"),  # Atención    → rojo claro
}

# Seguimiento en vivo de cambios de mesa: tras mover una cuenta, la tarjeta-preview de
# la mesa DESTINO muestra "(origen ➡️ destino)" en su texto inferior durante esta ventana
# (segundos). El monitor refresca cada 30 s, así que el rastro se ve unos refrescos y
# luego se borra solo (ver poda en _monitor_en_vivo).
TRANSFER_WINDOW = 120  # s

# Recordatorio de cambio: tras un cobro en efectivo con vuelto, pedidos.dialog_cobrar deja
# session_state['cambio_pendiente'] = {monto, titulo, ts}. El Monitor lo muestra como banner
# durante esta ventana (segundos) para que el cajero no olvide cuánto sacar del cajón; el
# refresco de 30 s lo deja expirar solo, y un botón "Entregado" lo descarta antes.
CAMBIO_WINDOW = 45  # s


def _mesa_corto(nombre) -> str:
    """Etiqueta corta de una mesa para la metadata de transferencia: los dígitos del
    nombre ('Mesa 5' → '5') o, si no los hay, el nombre tal cual ('Terraza')."""
    s = str(nombre or "").strip()
    return "".join(c for c in s if c.isdigit()) or s


def _banner_cambio(suf: str) -> None:
    """Banner persistente con el cambio a entregar tras un cobro en efectivo. Lo deja
    pedidos.dialog_cobrar en session_state; se muestra arriba del Monitor durante
    CAMBIO_WINDOW s (o hasta que el cajero pulse 'Entregado'). 'suf' hace única la clave
    del botón porque st.tabs ejecuta los dos fragmentos (salón y web) en cada render."""
    info = st.session_state.get("cambio_pendiente")
    if not info:
        return
    # Expira solo: el refresco de 30 s re-evalúa esto y lo descarta pasada la ventana.
    if time.time() - float(info.get("ts", 0)) > CAMBIO_WINDOW:
        st.session_state.pop("cambio_pendiente", None)
        return
    monto = int(info.get("monto", 0) or 0)
    titulo = html.escape(str(info.get("titulo", "")))
    c_msg, c_btn = st.columns([5, 1])
    with c_msg:
        st.markdown(
            '<div style="background:#fef3c7; border:1px solid #f59e0b; border-radius:14px; '
            'padding:0.9rem 1.2rem; display:flex; align-items:center; gap:14px;">'
            '<span style="font-size:1.9rem; line-height:1;">💵</span>'
            '<div><div style="font-family:\'DM Sans\',sans-serif; font-weight:700; '
            f'font-size:1.2rem; color:#92400e;">Entregar cambio: ${fmt_money(monto)}</div>'
            f'<div style="font-size:0.82rem; color:#b45309;">{titulo} · recuérdalo antes de '
            'cerrar el cajón</div></div></div>',
            unsafe_allow_html=True,
        )
    with c_btn:
        # Rerun COMPLETO (no de fragmento) para que el banner desaparezca de ambas pestañas.
        if st.button("✓ Entregado", key=f"cambio_ok_{suf}", use_container_width=True):
            st.session_state.pop("cambio_pendiente", None)
            _reanudar_refresco()   # ya entregó el cambio del cobro → reanuda el refresco en vivo
            st.rerun()
    st.markdown("<br>", unsafe_allow_html=True)


def _mesero_html(mesero) -> str:
    """Línea '🧑‍🍳 Tomó: <nombre>' para la tarjeta del pedido. Cadena vacía si la columna
    'mesero' viene NULL/NaN/vacía (pedidos del cliente por QR o app pública)."""
    try:
        if mesero is None or pd.isna(mesero):
            return ""
    except (TypeError, ValueError):
        pass
    nombre = str(mesero).strip()
    if not nombre:
        return ""
    return (f'<div style="font-size:0.78rem; color:#6b6b64; margin-top:2px;">'
            f'🧑‍🍳 Tomó: <b style="color:#45443e;">{html.escape(nombre)}</b></div>')


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


def _espera_mesa(sub: pd.DataFrame) -> int:
    """Minutos que la mesa lleva esperando: el pedido SIN ENTREGAR más antiguo. Es la
    clave de orden cronológico del monitor (la que más espera va primero). Las mesas con
    todo entregado (solo por cobrar) devuelven 0 → quedan después de las que aún esperan
    comida. -1 si no hay pedidos activos (mesa libre, no se muestra)."""
    if sub.empty:
        return -1
    pend = sub[sub["estado"] != "entregado"]
    if pend.empty:
        return 0
    esperas = [pedidos.minutos_espera(f) for f in pend["fecha"]]
    return max([m for m in esperas if m is not None], default=0)


# ── Modal: cambio de mesa (transferir cuenta a una mesa libre) ──────────────────
# Pop-up centrado: elige una mesa LIBRE y mueve allí todos los pedidos activos de la
# mesa de origen (FK atómica en pedidos.mover_mesa). Disponible para todo el personal
# (incl. mesero): reubicar comensales es una tarea de salón, no de cobro. Solo se
# ofrecen mesas libres como destino para no fusionar dos cuentas por accidente.
@st.dialog("🔀 Cambiar de mesa")
def _dialog_cambiar_mesa(origen_id: int, origen_nombre: str, ids, mesas_libres, uid: str):
    origen_id = int(origen_id)
    ids = [int(i) for i in ids]
    st.markdown(
        f"Mover los **{len(ids)} pedido(s) activo(s)** de "
        f"**🪑 {html.escape(str(origen_nombre))}** a otra mesa."
    )
    if not ids:
        st.info("Esta mesa no tiene pedidos activos que mover.")
        if st.button("Cerrar", key=f"cm_cerrar_{uid}", use_container_width=True):
            st.rerun()
        return
    if not mesas_libres:
        st.warning("No hay mesas libres disponibles como destino.")
        if st.button("Cerrar", key=f"cm_cerrar_{uid}", use_container_width=True):
            st.rerun()
        return

    opciones = {f"🪑 {m['nombre']}": int(m["id"]) for m in mesas_libres}
    destino_label = st.selectbox("Mesa destino (libre)", list(opciones.keys()),
                                 key=f"cm_destino_{uid}")
    st.caption("Solo se listan mesas libres para no fusionar dos cuentas por error.")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔀 Mover cuenta", key=f"cm_confirm_{uid}", type="primary",
                     use_container_width=True):
            destino_id = opciones[destino_label]
            destino_nombre = next((m["nombre"] for m in mesas_libres
                                   if int(m["id"]) == int(destino_id)), destino_label)
            movidos = pedidos.mover_mesa(ids, destino_id)
            st.session_state["mesa_activa"] = destino_id  # sigue al cliente a la nueva mesa
            # Rastro en vivo del cambio: la tarjeta-preview de la mesa destino mostrará
            # "(origen ➡️ destino)" durante TRANSFER_WINDOW (ver _monitor_en_vivo).
            st.session_state.setdefault("mesa_transfers", {})[int(destino_id)] = {
                "origen": _mesa_corto(origen_nombre),
                "destino": _mesa_corto(destino_nombre),
                "ts": time.time(),
            }
            pedidos.flash(
                f"{movidos} pedido(s) movido(s) · {origen_nombre} → {destino_label}", "🔀"
            )
            st.rerun()
    with c2:
        if st.button("Volver", key=f"cm_volver_{uid}", use_container_width=True):
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: MONITOR DE MESAS
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Objetivo 3: tarjetas de pedido AMPLIADAS en el monitor para leerse de lejos en un
    # entorno de ritmo alto. La clase 'mon-card' (añadida a las tarjetas del detalle y de
    # pedidos web) sube tamaño de fuente, interlineado y padding SIN tocar el .order-card
    # global del resto del panel.
    st.markdown("""
    <style>
    .mon-card { padding: 1.5rem 1.8rem !important; border-radius: 18px !important; }
    .mon-card .order-id    { font-size: 0.95rem !important; }
    .mon-card .order-num   { font-size: 1.25rem !important; font-weight: 600 !important; }
    .mon-card .order-items { font-size: 1.2rem !important; line-height: 1.55 !important; color: #26262b !important; }
    .mon-card .order-fecha { font-size: 0.9rem !important; }
    .mon-card .order-total { font-size: 1.7rem !important; }
    .mon-card .badge       { font-size: 0.95rem !important; padding: 5px 16px !important; }
    </style>
    """, unsafe_allow_html=True)

    # Pausa del refresco mientras hay una ventana abierta (ver _pedir_dialogo): run_every
    # se fija al CREAR el fragmento, así que lo decidimos aquí (scope app) según la bandera.
    # Con un diálogo abierto → run_every=None: el fragmento no se auto-relanza y no descuadra
    # el modal. Sin diálogo → "30s" en vivo, como siempre.
    rv = (None if (st.session_state.get("_mon_refresco_pausa")
                   or st.session_state.get("_edit_open")) else "30s")

    # Dos vistas aisladas: el salón (mesas) y los pedidos web (Domicilio / Para Llevar).
    tab_salon, tab_web = st.tabs(["🪑 Salón", "🛵 Pedidos web"])
    with tab_salon:
        # SOLO este fragmento se re-ejecuta en el intervalo (no toda la app). La mesa
        # seleccionada vive en session_state → se conserva entre refrescos.
        st.fragment(run_every=rv)(_monitor_en_vivo)()
    with tab_web:
        st.fragment(run_every=rv)(_web_en_vivo)()

    # Abre (una sola vez) el diálogo que pidió una tarjeta, ya FUERA de los fragmentos:
    # así es estable (como en caja) y los fragmentos quedan pausados mientras esté abierto.
    _abrir_dialogo_pendiente()

    # Ventana de edición de pedido web: despacho PERSISTENTE (se re-pinta en cada rerun
    # mientras esté abierta), también fuera de los fragmentos. La cierran sus botones.
    if st.session_state.get("_edit_open"):
        _dialog_editar(int(st.session_state["_edit_open"]),
                       st.session_state.get("_edit_uid", ""))


# ── Ventanas del monitor: abrir sin que el refresco de 30 s las descuadre ────────
# Los diálogos (cobrar, cambiar mesa, cancelar) viven dentro de los fragmentos run_every.
# Si el refresco salta con uno abierto, lo relanza y falla. Por eso las tarjetas NO abren
# el diálogo directamente: lo PIDEN (_pedir_dialogo) marcando una pausa y re-ejecutando la
# app; render() recrea los fragmentos pausados (run_every=None) y abre el diálogo fuera de
# ellos. Al cerrarlo (botones del propio diálogo) se reanuda el refresco.
def _pedir_dialogo(kind: str, **args) -> None:
    st.session_state["_mon_dialog"] = {"kind": kind, **args}
    st.session_state["_mon_refresco_pausa"] = True
    st.rerun()


def _reanudar_refresco() -> None:
    """Quita la pausa del refresco (lo llaman los diálogos al cerrarse y el cambio de mesa)."""
    st.session_state.pop("_mon_refresco_pausa", None)


def _abrir_dialogo_pendiente() -> None:
    d = st.session_state.get("_mon_dialog")
    if not d:
        return
    st.session_state["_mon_dialog"] = None   # one-shot: Streamlit mantiene el modal abierto
    kind = d.get("kind")
    if kind == "cobrar":
        pedidos.dialog_cobrar(d["ids"], d["titulo"], d["saldo"], d["uid"])
    elif kind == "cancelar":
        pedidos.dialog_cancelar(d["pid"], d["uid"])
    elif kind == "descuento":
        pedidos.dialog_descuento(d["pid"], d["saldo"], d["uid"])
    elif kind == "nota":
        pedidos.dialog_nota(d["pid"], d["num_dia"], d["estado"], d["uid"])
    elif kind == "cambiar_mesa":
        _dialog_cambiar_mesa(d["origen_id"], d["nombre"], d["ids"], d["mesas_libres"], d["uid"])


# ══════════════════════════════════════════════════════════════════════════════
# VENTANA: EDITAR PEDIDO DE ENTREGA (Domicilio / Para Llevar)
# ══════════════════════════════════════════════════════════════════════════════
# El cliente llama para agregar/quitar algo cuando el pedido ya está en cocina. Esta
# ventana edita una COPIA de trabajo de los items (st.session_state["_edit_items_<pid>"])
# y solo escribe al guardar (pedidos.actualizar_pedido_entrega, que ajusta el inventario
# por el delta de forma atómica).
#
# IMPORTANTE (Streamlit): dentro de un @st.dialog NO se puede usar st.rerun(scope="fragment")
# (solo vale en reruns de fragmento), así que NO se reutiliza el configurador del POS
# —que sí lo usa— ni se envuelve el diálogo en un fragmento. Los botones de +/−/agregar usan
# CALLBACKS on_click (mutan la copia de trabajo ANTES del re-render, manteniendo el modal
# abierto); guardar/cancelar sí cierran con st.rerun(). El diálogo se DESPACHA de forma
# PERSISTENTE (bandera "_edit_open") en render(): se vuelve a pintar en cada rerun mientras
# esté abierto. Todas las claves de estado llevan prefijo "_edit_" para aislarlas del POS.
def _editar_purgar(pid=None) -> None:
    """Baja la bandera de despacho y limpia TODO el estado de la ventana de edición (copia
    de trabajo, contadores y widgets). Solo hay una ventana abierta a la vez."""
    for k in [k for k in list(st.session_state) if str(k).startswith("_edit_")]:
        del st.session_state[k]


def _edit_agregar_catalogo(items: list, tipo: str, prod: dict) -> None:
    """Suma un producto de catálogo a la copia de trabajo: incrementa la línea si ya
    existe (mismo tipo+id y sin config), o agrega una línea nueva con cantidad 1."""
    pid_prod = int(prod["id"])
    for it in items:
        if (it.get("tipo") == tipo and int(it.get("id", -1)) == pid_prod
                and not it.get("config")):
            it["cantidad"] = int(it.get("cantidad", 1) or 1) + 1
            return
    items.append({"tipo": tipo, "id": pid_prod, "nombre": prod["nombre"],
                  "precio": int(prod["precio"]), "cantidad": 1})


# ── Callbacks on_click de la ventana de edición ─────────────────────────────────
# Mutan la copia de trabajo ANTES del re-render, así el modal queda abierto sin llamar a
# st.rerun (que cerraría el diálogo). Toleran índices viejos por si el estado cambió.
def _cb_edit_rm(pid, idx):
    items = st.session_state.get(f"_edit_items_{pid}")
    if items and 0 <= idx < len(items):
        items.pop(idx)


def _cb_edit_inc(pid, idx):
    items = st.session_state.get(f"_edit_items_{pid}")
    if items and 0 <= idx < len(items):
        it = items[idx]
        it["cantidad"] = int(it.get("cantidad", 1) or 1) + 1


def _cb_edit_dec(pid, idx):
    items = st.session_state.get(f"_edit_items_{pid}")
    if not items or not (0 <= idx < len(items)):
        return
    it = items[idx]
    c = int(it.get("cantidad", 1) or 1)
    if c > 1:
        it["cantidad"] = c - 1
    else:
        items.pop(idx)


def _cb_edit_dup(pid, idx):
    items = st.session_state.get(f"_edit_items_{pid}")
    if items and 0 <= idx < len(items):
        items.append(dict(items[idx]))


def _cb_edit_add_cat(pid, tipo, prod):
    items = st.session_state.get(f"_edit_items_{pid}")
    if items is not None:
        _edit_agregar_catalogo(items, tipo, prod)


def _cb_edit_acomp(pid, aid, delta, n):
    counts = st.session_state.setdefault(f"_edit_acomp_{pid}", {})
    if delta > 0 and sum(counts.values()) >= n:
        return
    c = counts.get(aid, 0) + delta
    if c <= 0:
        counts.pop(aid, None)
    else:
        counts[aid] = c


def _cb_edit_add_pd(pid, precio_pd, acomp_names, n):
    """Arma un Plato del Día desde los selectores + contadores y lo agrega a la copia de
    trabajo, luego reinicia el sub-formulario para el siguiente plato."""
    items = st.session_state.get(f"_edit_items_{pid}")
    if items is None:
        return
    counts = st.session_state.get(f"_edit_acomp_{pid}", {})
    if sum(counts.values()) != n:
        return

    def _val(g):
        v = st.session_state.get(f"_edit_pd_{g}_{pid}")
        return None if (v in (None, "Ninguno", "")) else v

    acomp_list = []
    for aid, c in counts.items():
        nombre = acomp_names.get(aid)
        if nombre:
            acomp_list += [nombre] * int(c)
    cfg = {"entrada": _val("entrada"), "principio": _val("principio"),
           "proteina": _val("proteina"), "acompanamientos": acomp_list}
    beb = _val("bebida")
    if beb:
        cfg["bebida"] = beb
    nota = (st.session_state.get(f"_edit_pd_nota_{pid}") or "").strip()
    items.append({"tipo": "plato_dia", "nombre": "Plato del Día", "precio": int(precio_pd),
                  "cantidad": 1, "config": cfg, "nota": nota})
    st.session_state[f"_edit_acomp_{pid}"] = {}
    st.session_state[f"_edit_pd_nota_{pid}"] = ""


def _editar_cerrar() -> None:
    """Cierra la ventana de edición: limpia el estado, reanuda el refresco y re-ejecuta."""
    _editar_purgar()
    _reanudar_refresco()
    st.rerun()


def _on_edit_dismiss() -> None:
    """El usuario cerró el modal con la ✕/Esc. Como el despacho es PERSISTENTE (bandera
    "_edit_open"), hay que limpiar el estado aquí o el diálogo se reabriría en el siguiente
    rerun. Streamlit re-ejecuta solo tras el callback, así que NO llamamos st.rerun."""
    _editar_purgar()
    _reanudar_refresco()


@st.dialog("✏️ Editar pedido", width="large", on_dismiss=_on_edit_dismiss)
def _dialog_editar(pid, uid):
    pid = int(pid)
    items_key = f"_edit_items_{pid}"

    # Lee el pedido del tablero (caché de 8 s). Si ya no está activo, se cerró/cobró.
    df = pedidos.cargar_pedidos()
    sub = df[df["id"] == pid] if (not df.empty and "id" in df.columns) else df.iloc[0:0]
    if sub.empty:
        st.info("Este pedido ya no está disponible para editar.")
        if st.button("Cerrar", key="_edit_close_btn", use_container_width=True):
            _editar_cerrar()
        return
    row = sub.iloc[0]
    estado = row.get("estado", "pendiente")
    num_dia = row.get("num_dia") or pid

    # Defensa en profundidad: la tarjeta ya oculta el botón fuera de la ventana editable.
    if not (pedidos.puede_cancelar(row) and estado in ("pendiente", "en preparacion")):
        st.warning("Ya no se puede editar este pedido (entró a caja o salió de cocina).")
        if st.button("Cerrar", key="_edit_close_btn", use_container_width=True):
            _editar_cerrar()
        return

    # Copia de trabajo (una sola vez por apertura): se edita en memoria y solo se persiste al
    # guardar. dict(it) por línea para no mutar la caché del tablero. Se siembran también la
    # nota y el cambio en sus claves de widget para precargarlos sin pelear con Streamlit.
    if not st.session_state.get(f"_edit_load_{pid}"):
        st.session_state[items_key] = [dict(it) for it in pedidos.parse_items(row.get("items"))]
        st.session_state[f"_edit_ng_{pid}"] = _txt(row.get("nota_general"))
        st.session_state[f"_edit_pc_{pid}"] = pedidos._a_entero(row.get("paga_con"))
        st.session_state[f"_edit_acomp_{pid}"] = {}
        st.session_state[f"_edit_load_{pid}"] = True
    items = st.session_state[items_key]

    df_cat    = npos.cargar_catalogo()
    comp      = npos.componentes_activos_por_grupo()
    precio_pd = npos.precio_plato_dia()
    n_ac      = npos.num_acompanamientos()

    st.markdown(f"**Pedido #{num_dia}** · {pedidos.formatear_fecha(row.get('fecha'))}")

    # ── Productos actuales (modificar / quitar) ─────────────────────────────────
    st.markdown('<div class="section-title">Productos del pedido</div>', unsafe_allow_html=True)
    if not items:
        st.markdown('<p style="color:#b45309; font-size:0.85rem;">El pedido quedó vacío. '
                    'Agrega productos abajo o cancela la edición.</p>', unsafe_allow_html=True)
    for idx in range(len(items)):
        it = items[idx]
        tipo   = it.get("tipo")
        nombre = str(it.get("nombre") or "?")
        precio = pedidos._a_entero(it.get("precio"))
        cant   = max(1, pedidos._a_entero(it.get("cantidad") or 1))
        cfg_txt = npos._cfg_text(it) if (tipo == "plato_dia" or it.get("config") or it.get("nota")) else ""
        c_info, c_ctrl = st.columns([3, 2])
        with c_info:
            extra = (f'<div style="font-size:0.74rem; color:#a3a39b;">{html.escape(cfg_txt)}</div>'
                     if cfg_txt else "")
            st.markdown(
                f'<div style="padding:6px 0;"><span style="font-size:0.9rem; color:#26262b;">'
                f'{cant}× {html.escape(nombre)}</span> '
                f'<span style="font-size:0.82rem; color:#6b6b64;">${fmt_money(precio * cant)}</span>'
                f'{extra}</div>', unsafe_allow_html=True)
        with c_ctrl:
            if tipo == "plato_dia":
                # Cada Plato del Día es una instancia con su propia config → quitar o duplicar.
                d1, d2 = st.columns(2)
                with d1:
                    st.button("🗑", key=f"_edit_rm_{pid}_{idx}", use_container_width=True,
                              help="Quitar este plato", on_click=_cb_edit_rm, args=(pid, idx))
                with d2:
                    st.button("＋ Otro", key=f"_edit_dup_{pid}_{idx}", use_container_width=True,
                              help="Agregar otro igual", on_click=_cb_edit_dup, args=(pid, idx))
            else:
                mcol, qcol, pcol = st.columns([1, 1, 1])
                with mcol:
                    st.button("−", key=f"_edit_dec_{pid}_{idx}", use_container_width=True,
                              on_click=_cb_edit_dec, args=(pid, idx))
                with qcol:
                    st.markdown(f'<div style="text-align:center; padding:4px 0; font-weight:600;">{cant}</div>',
                                unsafe_allow_html=True)
                with pcol:
                    st.button("+", key=f"_edit_inc_{pid}_{idx}", use_container_width=True,
                              on_click=_cb_edit_inc, args=(pid, idx))

    # ── Agregar productos ───────────────────────────────────────────────────────
    st.markdown('<div class="section-title">Agregar productos</div>', unsafe_allow_html=True)
    SECCIONES = [("especial", "⭐ Especiales"), ("a_la_carta", "🍽️ A la carta"),
                 ("adicional", "🍟 Adicionales"), ("bebida", "🥤 Bebidas")]
    for cat, label in SECCIONES:
        prods = npos._catalogo_seccion(df_cat, cat)
        if not prods:
            continue
        tipo_item = "item" if cat == "a_la_carta" else cat
        with st.expander(label):
            for p in prods:
                ca, cb = st.columns([4, 1])
                with ca:
                    desc = (f' · <span style="color:#a3a39b; font-style:italic;">'
                            f'{html.escape(str(p.get("descripcion")))}</span>'
                            if p.get("descripcion") else "")
                    st.markdown(
                        f'<div style="padding:4px 0; font-size:0.86rem;">'
                        f'{html.escape(str(p["nombre"]))} '
                        f'<span style="color:#6b6b64;">${fmt_money(p["precio"])}'
                        f'{npos._stock_suffix(p.get("stock"))}</span>{desc}</div>',
                        unsafe_allow_html=True)
                with cb:
                    st.button("➕", key=f"_edit_add_{pid}_{cat}_{p['id']}",
                              use_container_width=True, on_click=_cb_edit_add_cat,
                              args=(pid, tipo_item, p))

    # Configurador para AGREGAR un Plato del Día (selectores simples + contadores). No usa
    # los selectores de botón del POS porque esos llaman st.rerun(scope="fragment"), inválido
    # dentro de un diálogo. El "mitad y mitad" no está aquí: para combinar, agrega dos platos.
    pd_faltan = [g for g in ("entrada", "principio", "proteina", "acompanamiento")
                 if not comp.get(g)]
    if not pd_faltan:
        with st.expander("🍛 Agregar Plato del Día"):
            ent_opts = ["Ninguno"] + [a["nombre"] for a in comp["entrada"]]
            pri_opts = [a["nombre"] for a in comp["principio"]]
            pro_opts = [a["nombre"] for a in comp["proteina"]]
            st.selectbox("Entrada", ent_opts, key=f"_edit_pd_entrada_{pid}")
            st.selectbox("Principio", pri_opts, key=f"_edit_pd_principio_{pid}")
            st.selectbox("Carnes o Proteína", pro_opts, key=f"_edit_pd_proteina_{pid}")
            if comp.get("bebida"):
                beb_opts = ["Ninguno"] + [a["nombre"] for a in comp["bebida"]]
                st.selectbox("Bebida", beb_opts, key=f"_edit_pd_bebida_{pid}")

            counts = st.session_state.setdefault(f"_edit_acomp_{pid}", {})
            elegidos = sum(counts.values())
            acomp_names = {str(a["id"]): a["nombre"] for a in comp["acompanamiento"]}
            st.markdown(npos._grupo_label(f"Acompañamientos ({elegidos}/{n_ac})"),
                        unsafe_allow_html=True)
            for a in comp["acompanamiento"]:
                aid = str(a["id"])
                stock_a = a.get("stock")
                agot = npos.agotado_por_stock(stock_a)
                c = int(counts.get(aid, 0))
                ac1, ac2, ac3, ac4 = st.columns([3, 1, 1, 1])
                with ac1:
                    color = "#a3a39b" if agot else "#26262b"
                    st.markdown(f'<div style="padding:4px 0; font-size:0.86rem; color:{color};">'
                                f'{html.escape(str(a["nombre"]))}{npos._stock_suffix(stock_a)}</div>',
                                unsafe_allow_html=True)
                with ac2:
                    st.button("−", key=f"_edit_acm_{pid}_{aid}", use_container_width=True,
                              on_click=_cb_edit_acomp, args=(pid, aid, -1, n_ac))
                with ac3:
                    st.markdown(f'<div style="text-align:center; padding:4px 0; font-weight:600;">{c}</div>',
                                unsafe_allow_html=True)
                with ac4:
                    tope = (stock_a is not None and c >= int(stock_a))
                    st.button("+", key=f"_edit_acp_{pid}_{aid}", use_container_width=True,
                              disabled=(elegidos >= n_ac or agot or tope),
                              on_click=_cb_edit_acomp, args=(pid, aid, +1, n_ac))
            st.text_input("Nota del plato", key=f"_edit_pd_nota_{pid}",
                          label_visibility="collapsed",
                          placeholder="Nota para este plato (opcional)")
            st.button("➕ Agregar este plato del día", key=f"_edit_pd_add_{pid}",
                      use_container_width=True, disabled=(elegidos != n_ac),
                      on_click=_cb_edit_add_pd, args=(pid, precio_pd, acomp_names, n_ac))

    # ── Totales, nota y pago ────────────────────────────────────────────────────
    st.markdown("---")
    subtotal = sum(pedidos._a_entero(it.get("precio")) * max(1, pedidos._a_entero(it.get("cantidad") or 1))
                   for it in items)
    fee_unit = npos.fee_entrega()
    n_platos = npos._n_platos_recargo(items)
    fee   = fee_unit * n_platos
    total = subtotal + fee
    if fee:
        st.caption(f"Recargo de entrega: {n_platos} × ${fmt_money(fee_unit)} = ${fmt_money(fee)}")

    nota_general = st.text_input("Nota general del pedido", key=f"_edit_ng_{pid}",
                                 placeholder="Nota general (opcional)")

    metodo = _txt(row.get("metodo_pago"))
    paga_con_val = pedidos._a_entero(st.session_state.get(f"_edit_pc_{pid}"))
    if metodo == "efectivo":
        paga_con_val = int(st.number_input(
            "¿Con cuánto paga? (para el cambio)", min_value=0, step=1000, format="%d",
            key=f"_edit_pc_{pid}") or 0)
        if 0 < total <= paga_con_val:
            st.caption(f"Cambio: ${fmt_money(paga_con_val - total)}")

    st.markdown(
        f'<div style="display:flex; justify-content:space-between; padding:8px 0;">'
        f'<span style="font-family:\'DM Sans\',sans-serif; font-weight:600; color:#26262b;">Total</span>'
        f'<span style="font-family:\'DM Sans\',sans-serif; font-size:1.3rem; font-weight:600; '
        f'color:#26262b;">${fmt_money(total)}</span></div>', unsafe_allow_html=True)

    c_save, c_cancel = st.columns(2)
    with c_save:
        if st.button("💾 Guardar cambios", key="_edit_save_btn", type="primary",
                     use_container_width=True, disabled=(not items)):
            ok, msg = pedidos.actualizar_pedido_entrega(
                pid, items, total, fee, nota_general, paga_con_val)
            if ok:
                pedidos.flash(f"{msg} · Pedido #{num_dia}", "✏️")
                _editar_cerrar()
            else:
                st.error(msg)
    with c_cancel:
        if st.button("Cancelar", key="_edit_cancel_btn", use_container_width=True):
            _editar_cerrar()


def _monitor_en_vivo():
    _banner_cambio("salon")  # recordatorio de cambio del último cobro en efectivo
    st.markdown(titulo_seccion('🖥️ Monitor de mesas'), unsafe_allow_html=True)

    mesas = cargar_mesas_activas()
    if not mesas:
        st.markdown(
            '<p style="color:#a3a39b; font-size:0.85rem;">No hay mesas activas. '
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
    mesa_por_id = {int(m["id"]): m for m in mesas}
    por_mesa, color_por_mesa, estado_por_mesa = {}, {}, {}
    for m in mesas:
        mid = int(m["id"])
        sub = activos[activos["__mesa"] == mid].copy()
        por_mesa[mid] = sub
        color_por_mesa[mid], estado_por_mesa[mid] = _estado_mesa(sub)

    # Objetivos 1+2: SOLO las mesas con pedidos activos (las "Libre" se ocultan),
    # ordenadas cronológicamente — la que lleva más tiempo esperando comida sin entregar
    # va primero (_espera_mesa, desc). Las de "Por cobrar" (todo entregado) quedan al final.
    activas = [mid for mid in por_mesa if not por_mesa[mid].empty]
    activas.sort(key=lambda mid: _espera_mesa(por_mesa[mid]), reverse=True)

    # Saneamos la selección: si la mesa ya no tiene pedidos activos (se cobró/liberó) o
    # dejó de existir, la limpiamos — el monitor ya no la muestra.
    sel = st.session_state.get("mesa_activa")
    if sel is not None and sel not in activas:
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

    # ── CSS: botones-tarjeta de la fila superior (objetivo 3: horizontal) ───────
    st.markdown("""
    <style>
    /* Tarjetas-botón de mesa (fila horizontal). Sobrescriben el botón base. */
    [class*="st-key-mesabtn_"] button {
        text-align: left !important; justify-content: flex-start !important;
        padding: 12px 14px !important; min-height: 62px !important;
        border-radius: 12px !important; border: 1px solid #ececec !important;
        border-left: 4px solid #d8d6cf !important; background: #ffffff !important;
        color: #26262b !important; font-size: 0.86rem !important; font-weight: 600 !important;
        box-shadow: 0 1px 2px rgba(0,0,0,0.04) !important; margin: 0 0 8px 0 !important;
        line-height: 1.35 !important; white-space: normal !important;
    }
    [class*="st-key-mesabtn_"] button p { text-align: left !important; }
    [class*="st-key-mesabtn_"] button:hover {
        border-color: #a3a39b !important; background: #fafaf8 !important; color: #26262b !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # CSS dinámico: fondo COMPLETO por estado + resaltado de la mesa seleccionada.
    # Mismo nivel de especificidad que la regla base (declarado después → gana).
    dyn = []
    for mid in activas:
        color = color_por_mesa[mid]
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
            f".st-key-mesabtn_{sel} button {{ box-shadow:0 0 0 2px #26262b !important; "
            f"font-weight:800 !important; }}"
        )
    st.markdown(f"<style>{''.join(dyn)}</style>", unsafe_allow_html=True)

    # ── Objetivo 3: layout horizontal (fila de tarjetas activas) + detalle debajo ─
    if not activas:
        st.markdown(
            '<div style="border:1px dashed #d8d6cf; border-radius:14px; background:#ffffff; '
            'padding:2rem; text-align:center; color:#6b6b64; box-shadow:0 1px 2px rgba(0,0,0,0.04);">'
            '🟢 Todas las mesas están libres. Aparecerán aquí en cuanto tengan pedidos activos.</div>',
            unsafe_allow_html=True,
        )
        return

    DOT = {VERDE: "🟢", AMBAR: "🟠", AZUL: "🔵", ROJO: "🔴"}
    POR_FILA = 4  # tarjetas por fila; el resto se envuelve a la siguiente
    # Poda los rastros de transferencia vencidos (más viejos que TRANSFER_WINDOW): la
    # metadata "(origen ➡️ destino)" se borra sola y el estado no crece sin fin.
    now = time.time()
    _transfers = st.session_state.get("mesa_transfers")
    if _transfers:
        st.session_state["mesa_transfers"] = {
            k: v for k, v in _transfers.items() if now - v.get("ts", 0) <= TRANSFER_WINDOW
        }
    transfers = st.session_state.get("mesa_transfers", {})
    for inicio in range(0, len(activas), POR_FILA):
        fila = activas[inicio:inicio + POR_FILA]
        cols = st.columns(POR_FILA, gap="small")
        for col, mid in zip(cols, fila):
            with col:
                nombre = str(mesa_por_id[mid]["nombre"])
                sub    = por_mesa[mid]
                color  = color_por_mesa[mid]
                saldo  = int(sub.apply(saldo_pedido, axis=1).sum())
                etiqueta = "por cobrar" if color == AZUL else "pedido(s)"
                espera = _espera_mesa(sub)
                chip = f" · ⏱ {espera}m" if espera > 0 else ""
                resumen = f"{len(sub)} {etiqueta} · ${fmt_money(saldo)}{chip}"
                # Rastro de cambio de mesa en vivo: si esta mesa recibió una cuenta hace
                # poco, añade "(origen ➡️ destino)" al texto inferior de su preview.
                tr = transfers.get(mid)
                if tr:
                    resumen += f" ({tr.get('origen', '?')} ➡️ {tr.get('destino', '?')})"
                if st.button(f"{DOT[color]}  {nombre}\n\n{resumen}", key=f"mesabtn_{mid}",
                             use_container_width=True):
                    st.session_state["mesa_activa"] = mid
                    _reanudar_refresco()   # navegar = reanuda el refresco (autocura tras una X)
                    st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    # Detalle de la mesa seleccionada, a todo el ancho debajo de la fila.
    if sel is None:
        _placeholder()
    else:
        mesa = mesa_por_id.get(sel)
        # Mesas libres (verde) como destinos válidos del cambio de mesa, excluida la
        # propia mesa seleccionada. Solo libres → nunca se fusionan dos cuentas.
        mesas_libres = [
            {"id": int(m["id"]), "nombre": str(m["nombre"])}
            for m in mesas
            if color_por_mesa[int(m["id"])] == VERDE and int(m["id"]) != sel
        ]
        _detalle_mesa(sel, str(mesa["nombre"]) if mesa else f"Mesa {sel}",
                      por_mesa[sel], color_por_mesa[sel], estado_por_mesa[sel],
                      df, mesas_libres)


# ── Detalle: placeholder (sin mesa) ─────────────────────────────────────────────
def _placeholder():
    st.markdown("""
    <div style="border:1px dashed #d8d6cf; border-radius:16px; background:#ffffff;
                padding:4rem 2rem; text-align:center; margin-top:0.5rem;
                box-shadow:0 1px 2px rgba(0,0,0,0.04);">
      <div style="font-size:2.6rem; margin-bottom:0.6rem;">🍽️</div>
      <div style="font-family:'DM Sans',sans-serif; font-size:1.15rem; font-weight:600; color:#26262b;">
        Selecciona una mesa
      </div>
      <div style="font-size:0.85rem; color:#a3a39b; margin-top:6px;">
        Elige una mesa en la fila de arriba para ver los detalles de su pedido.
      </div>
    </div>
    """, unsafe_allow_html=True)


# ── Detalle: ambiente de una mesa ───────────────────────────────────────────────
def _detalle_mesa(mid: int, nombre: str, sub: pd.DataFrame, color: str,
                  estado_txt: str, df_full: pd.DataFrame, mesas_libres=None):
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
          <div style="font-family:'DM Sans',sans-serif; font-size:1.4rem; font-weight:600; color:#26262b;">🪑 {html.escape(nombre)}</div>
          <div style="font-size:0.8rem; color:{color}; font-weight:600; margin-top:2px;">{html.escape(estado_txt)}</div>
        </div>
        <div style="text-align:right;">
          <div class="metric-label">Por cobrar</div>
          <div class="order-total" style="font-size:1.4rem;">${fmt_money(saldo_activo)}</div>
          <div style="font-size:0.72rem; color:#a3a39b; margin-top:4px;">Cobradas hoy: {cerradas_n} · ${fmt_money(cerradas_total)}</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Acciones de mesa. El cobro abre el modal compartido (efectivo/transferencia,
    # cambio y abonos parciales). Cobra contra los ids visibles de 'sub' (así también
    # entran los pedidos heredados con mesa_id NULL); un abono parcial deja la mesa
    # abierta, un pago completo la libera.
    a1, a2, a3 = st.columns([2, 2, 1])
    with a1:
        # Cobrar es capacidad bloqueada para el mesero (monitor de solo visualización).
        if not sub.empty and auth.can("cobrar"):
            if st.button("💵 Cobrar mesa", key=f"mon_cobrar_mesa_{mid}",
                         type="primary", use_container_width=True):
                _pedir_dialogo("cobrar", ids=[int(i) for i in sub["id"].tolist()],
                               titulo=nombre, saldo=int(saldo_activo), uid=f"mesa_{mid}")
    with a2:
        # Cambio de mesa: mueve la cuenta a una mesa libre. Disponible para todo el
        # personal (reubicar comensales es tarea de salón). Solo si la mesa tiene
        # pedidos activos que mover.
        if not sub.empty:
            if st.button("🔀 Cambiar de mesa", key=f"mon_cambiar_mesa_{mid}",
                         use_container_width=True):
                _pedir_dialogo("cambiar_mesa", origen_id=int(mid), nombre=nombre,
                               ids=[int(i) for i in sub["id"].tolist()],
                               mesas_libres=(mesas_libres or []), uid=f"mesa_{mid}")
    with a3:
        if st.button("✕ Deseleccionar", key=f"mon_deselect_{mid}",
                     use_container_width=True):
            st.session_state.pop("mesa_activa", None)
            _reanudar_refresco()
            st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    if sub.empty:
        st.markdown(
            '<p style="color:#a3a39b; font-size:0.9rem; padding:1.5rem 0; text-align:center;">'
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
    num_dia = row.get("num_dia") or pid   # número diario; fallback al id global (pre-migración)
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
    abono_html = (f'<div style="font-size:0.72rem; color:#4b43b0; font-weight:600; margin-top:2px;">'
                  f'Abonado ${fmt_money(abonado)} de ${fmt_money(total_p)}</div>'
                  if abonado > 0 else "")

    # Auto-servicio por QR: chip distintivo junto al n.º de pedido para que el mesero sepa
    # que lo pidió el propio comensal desde la mesa (req #4).
    es_qr = str(row.get("tipo_entrega") or "") == "mesa_qr"
    qr_chip = ('<span style="display:inline-block; background:#e9e7fb; color:#4b43b0; '
               'border:1px solid #d6d2f5; border-radius:999px; padding:1px 8px; '
               'font-size:0.68rem; font-weight:700; margin-left:6px; vertical-align:middle;">'
               '📲 QR auto-servicio</span>') if es_qr else ""

    # Quién tomó el pedido (columna 'mesero'): NULL/vacío en los de QR/cliente → no se muestra.
    mesero_html = _mesero_html(row.get("mesero"))
    # Nota del pedido (puede añadirse/editarse tras enviarlo con 📝 Nota).
    nota_txt = _txt(row.get("nota_general")).strip()
    nota_html = (f'<div style="font-size:0.8rem; color:#b45309; margin-top:4px;">📝 '
                 f'{html.escape(nota_txt)}</div>') if nota_txt else ""

    st.markdown(f"""
    <div class="order-card mon-card"{borde}>
      <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
          <div class="order-id">Pedido #{num_dia}{qr_chip}</div>{mesero_html}
          <div class="order-items">{items}</div>{nota_html}
          <div class="order-fecha">{fecha}</div>
        </div>
        <div style="text-align:right;">{pedidos.badge_html(estado)}<div class="order-total" style="margin-top:8px;">${fmt_money(saldo)}</div>{abono_html}{chip}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    b1, b2, b3, b4, b5, b6, b7 = st.columns(7)
    with b1:
        btn_label = pedidos.ESTADO_LABEL_BTN.get(estado)
        if btn_label and st.button(btn_label, key=f"avanzar_{uid}", type="primary",
                                   use_container_width=True):
            pedidos.avanzar_estado(pid, estado)  # flashea toast + st.rerun()
    with b2:
        if st.button("🖨 Ticket", key=f"ticket_{uid}", use_container_width=True,
                     help="Imprimir el prerecibo (pre-cuenta) del cliente"):
            pedidos.enqueue_prerecibo(pid)
            pedidos.flash(f"Prerecibo enviado · Pedido #{pid}", "🖨")
            st.rerun()
    with b3:
        if auth.can("cobrar") and st.button(
                "💵 Cobrar", key=f"cobrar_{uid}", use_container_width=True,
                help="Cobrar este pedido (efectivo/transferencia, abono parcial)",
                disabled=saldo <= 0):
            _pedir_dialogo("cobrar", ids=[int(pid)], titulo=f"Pedido #{num_dia}",
                           saldo=int(saldo), uid=uid)
    with b4:
        # Descuento / cortesía: solo cajero/admin y con saldo; el modal exige PIN de admin.
        if auth.can("cobrar") and saldo > 0 and st.button(
                "🏷️ Descuento", key=f"descuento_{uid}", use_container_width=True,
                help="Descuento o cortesía (requiere PIN de administrador)"):
            _pedir_dialogo("descuento", pid=int(pid), saldo=int(saldo), uid=uid)
    with b5:
        # Reimprimir la comanda de cocina (atasco / ticket perdido) sin cambiar de estado.
        # Solo aplica a pedidos en cocina (ESTADOS_ACTIVOS).
        if estado in pedidos.ESTADOS_ACTIVOS and st.button(
                "🍳 Comanda", key=f"comanda_{uid}", use_container_width=True,
                help="Reimprimir la comanda de cocina"):
            pedidos.enqueue_comanda(pid)
            pedidos.flash(f"Comanda reenviada · Pedido #{pid}", "🍳")
            st.rerun()
    with b6:
        # Añadir/editar la nota del pedido ya enviado (sin candado de rol: tarea de salón).
        if st.button("📝 Nota", key=f"nota_{uid}", use_container_width=True,
                     help="Añadir o editar la nota del pedido"):
            _pedir_dialogo("nota", pid=int(pid), num_dia=num_dia, estado=estado, uid=uid)
    with b7:
        # Anti-skimming: si la cuenta ya tocó caja (cobro iniciado / abono / pago), el
        # botón queda BLOQUEADO. Fase 3: modal centrado compartido con el tablero.
        if pedidos.puede_cancelar(row):
            if st.button("✕ Cancelar", key=f"cancelar_{uid}", use_container_width=True):
                _pedir_dialogo("cancelar", pid=int(pid), uid=uid)
        else:
            st.button("🔒 En caja", key=f"cancelar_{uid}", use_container_width=True,
                      disabled=True,
                      help="No se puede cancelar: la cuenta ya entró a caja (anti-fraude).")


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: PEDIDOS WEB (Domicilio / Para Llevar) — vista de despacho aislada
# ══════════════════════════════════════════════════════════════════════════════
# Los pedidos de la app pública llevan tipo_entrega = 'domicilio' | 'para_llevar' y
# no tienen mesa, así que no aparecen en el salón. Aquí la cocina los ve juntos con
# todo lo necesario para prepararlos y despacharlos: contacto, dirección (domicilio),
# método de pago + cambio, recargo de envío y las mismas acciones del tablero.
TIPO_BADGE = {
    "domicilio":   ("🛵 Domicilio",   "#e9e7fb", "#4b43b0"),
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


def _web_en_vivo():
    _banner_cambio("web")  # mismo recordatorio de cambio en la pestaña de pedidos web
    st.markdown(titulo_seccion('🛵 Pedidos web · Domicilio y Para Llevar'),
                unsafe_allow_html=True)

    df = pedidos.cargar_pedidos()
    pedidos._maybe_print_ticket(df)  # impresión bajo demanda (un solo iframe)

    if "tipo_entrega" not in df.columns:
        st.markdown('<p style="color:#a3a39b; font-size:0.85rem;">Aún no hay pedidos web.</p>',
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

    st.markdown('<p style="color:#a3a39b; font-size:0.78rem; margin-top:6px;">No ocupan mesa. '
                'Prepáralos y despáchalos desde aquí.</p>', unsafe_allow_html=True)

    if activos.empty:
        st.markdown('<p style="color:#a3a39b; font-size:0.9rem; padding:1.5rem 0; text-align:center;">'
                    'No hay pedidos web en curso.</p>', unsafe_allow_html=True)
        return

    activos = activos.sort_values("fecha")  # más antiguo primero (urgencia de despacho)
    for idx, (_, row) in enumerate(activos.iterrows()):
        _web_card(row, idx)


def _web_card(row, idx: int):
    pid      = int(row["id"])
    num_dia  = row.get("num_dia") or pid
    estado   = row.get("estado", "pendiente")
    tipo     = str(row.get("tipo_entrega") or "")
    etiqueta, bg, fg = TIPO_BADGE.get(tipo, ("Web", "#ececec", "#45443e"))
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
    fee_html = (f'<div style="font-size:0.72rem; color:#a3a39b;">incl. envío ${fmt_money(fee)}</div>'
                if fee else "")

    # Una SOLA cadena sin saltos de línea ni sangría: una interpolación vacía (sin nota,
    # sin dirección…) dejaba una línea en blanco que cerraba el bloque HTML y Markdown
    # pintaba el resto como bloque de código (HTML crudo visible). Concatenar lo evita.
    st.markdown(
        f'<div class="order-card mon-card"{borde}>'
        f'<div style="display:flex; justify-content:space-between; align-items:flex-start;">'
        f'<div>'
        f'<span class="badge" style="background:{bg}; color:{fg}; border:1px solid {bg};">{etiqueta}</span>'
        f'<div class="order-id" style="margin-top:6px;">Pedido #{num_dia}</div>'
        f'{_mesero_html(row.get("mesero"))}'
        f'<div class="order-num">{contacto}</div>'
        f'{dir_html}'
        f'<div class="order-items">{items}</div>'
        f'<div style="font-size:0.78rem; color:#6b6b64; margin-top:4px;">{pago_html}</div>'
        f'{nota_html}'
        f'<div class="order-fecha">{fecha}</div>'
        f'</div>'
        f'<div style="text-align:right;">'
        f'{pedidos.badge_html(estado)}'
        f'<div class="order-total" style="margin-top:8px;">${fmt_money(total)}</div>'
        f'{fee_html}{chip}'
        f'</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    b1, b2, b3, b4, b5, b6, b7, b8 = st.columns(8)
    with b1:
        btn_label = pedidos.ESTADO_LABEL_BTN.get(estado)
        if btn_label and st.button(btn_label, key=f"avanzar_{uid}", type="primary",
                                   use_container_width=True):
            pedidos.avanzar_estado(pid, estado)  # flashea toast + st.rerun()
    with b2:
        if st.button("🖨 Ticket", key=f"ticket_{uid}", use_container_width=True,
                     help="Imprimir el prerecibo (pre-cuenta) del cliente"):
            pedidos.enqueue_prerecibo(pid)
            pedidos.flash(f"Prerecibo enviado · Pedido #{pid}", "🖨")
            st.rerun()
    with b3:
        if auth.can("cobrar") and st.button(
                "💵 Cobrar", key=f"cobrar_{uid}", use_container_width=True,
                disabled=saldo <= 0):
            _pedir_dialogo("cobrar", ids=[int(pid)], titulo=f"Pedido #{num_dia}",
                           saldo=int(saldo), uid=uid)
    with b4:
        if auth.can("cobrar") and saldo > 0 and st.button(
                "🏷️ Descuento", key=f"descuento_{uid}", use_container_width=True,
                help="Descuento o cortesía (requiere PIN de administrador)"):
            _pedir_dialogo("descuento", pid=int(pid), saldo=int(saldo), uid=uid)
    with b5:
        # Reimprimir la comanda de cocina del pedido web (atasco / ticket perdido).
        if estado in pedidos.ESTADOS_ACTIVOS and st.button(
                "🍳 Comanda", key=f"comanda_{uid}", use_container_width=True,
                help="Reimprimir la comanda de cocina"):
            pedidos.enqueue_comanda(pid)
            pedidos.flash(f"Comanda reenviada · Pedido #{pid}", "🍳")
            st.rerun()
    with b6:
        # Añadir/editar la nota del pedido web ya enviado (cambio de último momento).
        if st.button("📝 Nota", key=f"nota_{uid}", use_container_width=True,
                     help="Añadir o editar la nota del pedido"):
            _pedir_dialogo("nota", pid=int(pid), num_dia=num_dia, estado=estado, uid=uid)
    with b7:
        # Editar items (agregar/quitar/modificar) de un pedido de entrega que aún está en
        # preparación y no ha tocado caja. Tras cobrar/abonar queda bloqueado (igual que cancelar).
        editable = pedidos.puede_cancelar(row) and estado in ("pendiente", "en preparacion")
        if editable:
            if st.button("✏️ Editar", key=f"editar_{uid}", use_container_width=True,
                         help="Agregar o modificar productos del pedido"):
                # Despacho PERSISTENTE (no el one-shot _pedir_dialogo): el editor usa
                # callbacks on_click y debe re-pintarse en cada rerun mientras esté abierto.
                st.session_state["_edit_open"] = int(pid)
                st.session_state["_edit_uid"] = uid
                st.session_state["_mon_refresco_pausa"] = True
                st.rerun()
        else:
            st.button("✏️ Editar", key=f"editar_{uid}", use_container_width=True,
                      disabled=True,
                      help="Solo se puede editar mientras está en preparación y antes de cobrar.")
    with b8:
        # Anti-skimming: bloqueado si la cuenta ya tocó caja (cobro iniciado / abono / pago).
        if pedidos.puede_cancelar(row):
            if st.button("✕ Cancelar", key=f"cancelar_{uid}", use_container_width=True):
                _pedir_dialogo("cancelar", pid=int(pid), uid=uid)
        else:
            st.button("🔒 En caja", key=f"cancelar_{uid}", use_container_width=True,
                      disabled=True,
                      help="No se puede cancelar: la cuenta ya entró a caja (anti-fraude).")
