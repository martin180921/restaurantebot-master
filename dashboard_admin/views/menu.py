"""Vista de Menú: administración de las 4 secciones de la carta + ajustes.

Pestañas (la misma taxonomía que ven el POS y la app del cliente):
  🍽️ Plato del Día  → componentes por grupo (entrada / principio / proteína /
                       acompañamientos). Cada opción se activa, se marca "86" (agotado
                       hoy), se renombra o se elimina. Precio plano editable en Ajustes.
  ⭐ Especiales     → platos con descripción de lo que incluyen; precio plano de la
                       categoría (todos cuestan igual).
  📋 A la carta     → platos sueltos con su propio precio.
  🥤 Bebidas        → bebidas con su propio precio.
  ⚙️ Ajustes        → precios planos (Plato del Día y Especiales), recargo de entrega
                       (Domicilio / Para Llevar) y nº de acompañamientos a elegir.

Reusa los patrones del panel: tarjetas (.menu-card), modales @st.dialog, toasts
db.flash() y la invalidación de caché (cargar_*.clear()) tras cada escritura.
"""
import streamlit as st
from sqlalchemy import text
import html
import io
import csv
import numbers
import unicodedata
import pandas as pd
from datetime import date

import auth
from db import (engine, titulo_seccion, cargar_menu, cargar_componentes, cargar_catalogo,
                cargar_ajustes, fmt_money, flash, disponibles,
                componentes_activos_por_grupo, precio_plato_dia, num_acompanamientos,
                GRUPOS_COMPONENTE, GRUPO_LABEL, precio_especiales,
                guardar_inventario, stock_int, STOCK_BAJO, agotado_por_stock)


# ── Helpers ──────────────────────────────────────────────────────────────────────
def _txt(valor) -> str:
    """Coerción segura a str para celdas que pueden venir None/NaN (descripcion)."""
    if valor is None:
        return ""
    try:
        if pd.isna(valor):
            return ""
    except (TypeError, ValueError):
        pass
    return str(valor)


def _agotado_hoy(valor) -> bool:
    """True si agotado_hasta cubre el día de hoy (patrón "86")."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return False
    try:
        return pd.Timestamp(valor).date() >= date.today()
    except (ValueError, TypeError):
        return False


def _clear_menu_caches():
    """Invalida las cachés que dependen de la tabla 'menu' (catálogo nuevo + lectura
    plana heredada que aún usa el POS hasta su rediseño)."""
    cargar_catalogo.clear()
    cargar_menu.clear()


def _stock_chip(stock) -> str:
    """Chip de existencias para las tarjetas: '📦 N' verde (>umbral) / ámbar (≤umbral) /
    rojo (0). Vacío si la opción no lleva control de stock (ilimitada). El stock diario
    se fija en la pestaña 📦 Inventario."""
    s = stock_int(stock)
    if s is None:
        return ""
    if s <= 0:
        bg, fg = "#fee2e2", "#991b1b"
    elif s <= STOCK_BAJO:
        bg, fg = "#fef3c7", "#92400e"
    else:
        bg, fg = "#dcfce7", "#15803d"
    return (f'<span class="badge" style="background:{bg}; color:{fg}; border:1px solid {bg};">'
            f'📦 {s}</span>')


# ── Disponibilidad y acordeón del menú (UI) ──────────────────────────────────────
def _disponible_hoy(row) -> bool:
    """True si la fila (plato o componente) se puede SERVIR hoy: activa, sin 86 manual
    del día y con stock (o ilimitada). Es el criterio para subirla a la sección superior
    'Disponibles' de su categoría; lo demás (inactivo / 86 / agotado) baja al fondo."""
    return (bool(row["activo"])
            and not _agotado_hoy(row.get("agotado_hasta"))
            and not agotado_por_stock(row.get("stock")))


def _partir_disponibles(sub):
    """(disponibles, no_disponibles) conservando el orden por 'orden' dentro de cada
    grupo (sub ya viene ordenado por cargar_catalogo()/cargar_componentes())."""
    disp, no = [], []
    if sub is not None and not sub.empty:
        for _, row in sub.iterrows():
            (disp if _disponible_hoy(row) else no).append(row)
    return disp, no


def _resumen_cat(sub) -> str:
    """Texto para la cabecera de un acordeón de categoría: 'N disponibles · M no disp.'."""
    disp, no = _partir_disponibles(sub)
    if not disp and not no:
        return "vacío"
    txt = f"{len(disp)} disponible" + ("s" if len(disp) != 1 else "")
    if no:
        txt += f" · {len(no)} no disp."
    return txt


def _subseccion(texto: str, color: str) -> str:
    """Encabezado de sub-sección (Disponibles / No disponibles) dentro de una categoría."""
    return (f'<div style="font-family:\'DM Sans\',sans-serif; font-size:0.78rem; '
            f'font-weight:600; color:{color}; letter-spacing:0.05em; '
            f'text-transform:uppercase; margin:0.7rem 0 0.5rem 0;">{texto}</div>')


def _inject_accordion_css():
    """Estilo de las cabeceras de acordeón del Menú (botones con key 'acc_*'): ancho
    completo, alineadas a la izquierda y con aspecto de cabecera plegable (no de botón).
    Se inyecta DESPUÉS del CSS global de panel.py; la variante anclada en
    div[data-testid="stColumn"] iguala/gana la especificidad de las reglas de columna
    (mismo patrón que los botones semánticos del panel)."""
    st.markdown("""
    <style>
    [class*="st-key-acc_"] button,
    div[data-testid="stColumn"] [class*="st-key-acc_"] .stButton > button {
        text-align: left !important; justify-content: flex-start !important;
        background: #fbfbf9 !important; border: 1px solid #ececec !important;
        border-radius: 12px !important; padding: 12px 16px !important;
        font-size: 0.95rem !important; font-weight: 600 !important;
        color: #26262b !important; margin: 6px 0 !important; min-height: 0 !important;
    }
    [class*="st-key-acc_"] button:hover,
    div[data-testid="stColumn"] [class*="st-key-acc_"] .stButton > button:hover {
        background: #f2f1ed !important; border-color: #d8d6cf !important; color: #26262b !important;
    }
    [class*="st-key-acc_"] button p,
    div[data-testid="stColumn"] [class*="st-key-acc_"] .stButton > button p {
        text-align: left !important; margin: 0 !important; width: 100% !important; font-weight: 600 !important;
    }
    </style>
    """, unsafe_allow_html=True)


def _acc_header(key: str, label: str, resumen: str = "") -> bool:
    """Cabecera de una sección de acordeón. El estado abierto/cerrado vive en
    session_state['acc_open_<key>'] para SOBREVIVIR a los st.rerun() de cada acción de la
    tarjeta (activar / 86 / editar / eliminar): st.expander reinicia su estado en cada
    rerun, por eso aquí se gestiona a mano. Devuelve True si la sección queda desplegada."""
    open_key = f"acc_open_{key}"
    abierto = bool(st.session_state.get(open_key, False))
    chevron = "▾" if abierto else "▸"
    extra = f"   ·   {resumen}" if resumen else ""
    if st.button(f"{chevron}   {label}{extra}", key=f"acc_{key}", use_container_width=True):
        st.session_state[open_key] = not abierto
        st.rerun()
    return bool(st.session_state.get(open_key, False))


# ══════════════════════════════════════════════════════════════════════════════
# DB: componentes del Plato del Día (tabla menu_componentes)
# ══════════════════════════════════════════════════════════════════════════════
def agregar_componente(grupo: str, nombre: str):
    with engine.begin() as conn:
        mx = conn.execute(text(
            "SELECT COALESCE(MAX(orden),0) FROM menu_componentes WHERE grupo = :g"
        ), {"g": grupo}).scalar()
        conn.execute(text(
            "INSERT INTO menu_componentes (grupo, nombre, activo, orden) "
            "VALUES (:g, :n, TRUE, :o)"
        ), {"g": grupo, "n": nombre.strip(), "o": mx + 1})
    cargar_componentes.clear()
    flash("Opción agregada", "✅")


def actualizar_componente(cid: int, nombre: str):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu_componentes SET nombre = :n WHERE id = :id"),
                     {"n": nombre.strip(), "id": cid})
    cargar_componentes.clear()
    flash("Opción actualizada", "✅")


def toggle_componente(cid: int, activo_actual: bool):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu_componentes SET activo = :a WHERE id = :id"),
                     {"a": not activo_actual, "id": cid})
    cargar_componentes.clear()
    flash("Opción activada" if not activo_actual else "Opción desactivada",
          "▶️" if not activo_actual else "⏸️")


def eliminar_componente(cid: int):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM menu_componentes WHERE id = :id"), {"id": cid})
    cargar_componentes.clear()
    flash("Opción eliminada", "🗑️")


def marcar_agotado_componente(cid: int):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu_componentes SET agotado_hasta = CURRENT_DATE WHERE id = :id"),
                     {"id": cid})
    cargar_componentes.clear()
    flash("Marcado agotado por hoy", "🚫")


def quitar_agotado_componente(cid: int):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu_componentes SET agotado_hasta = NULL WHERE id = :id"),
                     {"id": cid})
    cargar_componentes.clear()
    flash("Disponible de nuevo", "♻️")


# ══════════════════════════════════════════════════════════════════════════════
# DB: catálogo por categoría (tabla menu: especial / a_la_carta / bebida)
# ══════════════════════════════════════════════════════════════════════════════
def agregar_item(categoria: str, nombre: str, precio: int, descripcion=None):
    # Especiales: precio plano de la categoría (se ignora cualquier precio entrante).
    precio = precio_especiales() if categoria == "especial" else int(precio)
    with engine.begin() as conn:
        mx = conn.execute(text("SELECT COALESCE(MAX(orden),0) FROM menu WHERE categoria = :c"),
                          {"c": categoria}).scalar()
        conn.execute(text(
            "INSERT INTO menu (nombre, precio, activo, orden, categoria, descripcion) "
            "VALUES (:n, :p, TRUE, :o, :c, :d)"
        ), {"n": nombre.strip(), "p": int(precio), "o": mx + 1, "c": categoria,
            "d": (descripcion or None)})
    _clear_menu_caches()
    flash("Plato agregado", "✅")


def actualizar_item(item_id: int, nombre: str, precio: int, descripcion=None):
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE menu SET nombre = :n, precio = :p, descripcion = :d WHERE id = :id"
        ), {"n": nombre.strip(), "p": int(precio), "d": (descripcion or None), "id": item_id})
    _clear_menu_caches()
    flash("Plato actualizado", "✅")


def toggle_item(item_id: int, activo_actual: bool):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu SET activo = :a WHERE id = :id"),
                     {"a": not activo_actual, "id": item_id})
    _clear_menu_caches()
    flash("Plato activado" if not activo_actual else "Plato desactivado",
          "▶️" if not activo_actual else "⏸️")


def eliminar_item(item_id: int):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM menu WHERE id = :id"), {"id": item_id})
    _clear_menu_caches()
    flash("Plato eliminado", "🗑️")


def marcar_agotado_item(item_id: int):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu SET agotado_hasta = CURRENT_DATE WHERE id = :id"),
                     {"id": item_id})
    _clear_menu_caches()
    flash("Marcado agotado por hoy", "🚫")


def quitar_agotado_item(item_id: int):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu SET agotado_hasta = NULL WHERE id = :id"),
                     {"id": item_id})
    _clear_menu_caches()
    flash("Disponible de nuevo", "♻️")


# ── Edición de stock por ítem (desde la tarjeta del menú, no solo en 📦 Inventario) ──
# El indicador de stock de cada tarjeta es además el editor: stock None = ilimitado;
# un número = porciones de hoy (0 = agotado). Invalida las cachés y refresca igual que
# el resto de acciones de la tarjeta (activar / 86 / editar).
def guardar_stock_componente(cid: int, stock):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu_componentes SET stock = :s WHERE id = :id"),
                     {"s": (None if stock is None else int(stock)), "id": int(cid)})
    cargar_componentes.clear()
    flash("Stock actualizado", "📦")


def guardar_stock_item(item_id: int, stock):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu SET stock = :s WHERE id = :id"),
                     {"s": (None if stock is None else int(stock)), "id": int(item_id)})
    _clear_menu_caches()
    flash("Stock actualizado", "📦")


# ══════════════════════════════════════════════════════════════════════════════
# DB: ajustes (precios planos + recargo de entrega)
# ══════════════════════════════════════════════════════════════════════════════
def guardar_ajustes(d: dict):
    with engine.begin() as conn:
        for clave, valor in d.items():
            conn.execute(text(
                "INSERT INTO ajustes (clave, valor) VALUES (:k, :v) "
                "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor"
            ), {"k": clave, "v": str(valor)})
        # El precio de Especiales es plano: al cambiarlo, alinea TODAS las especiales
        # (mantiene menu.precio usable en cualquier lectura, incl. la heredada).
        if "especiales_precio" in d:
            conn.execute(text("UPDATE menu SET precio = :p WHERE categoria = 'especial'"),
                         {"p": int(d["especiales_precio"])})
    cargar_ajustes.clear()
    _clear_menu_caches()
    flash("Ajustes guardados", "✅")


# ══════════════════════════════════════════════════════════════════════════════
# Modales (Plato del Día: renombrar / eliminar opción)
# ══════════════════════════════════════════════════════════════════════════════
@st.dialog("Editar opción")
def _dialog_editar_componente(cid: int, nombre_actual: str):
    nuevo = st.text_input("Nombre", value=nombre_actual)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("💾 Guardar", key=f"btn_guardar_comp_{cid}", type="primary",
                     use_container_width=True):
            if nuevo.strip():
                actualizar_componente(cid, nuevo)
                st.rerun()
            else:
                st.error("El nombre no puede estar vacío.")
    with c2:
        if st.button("Volver", key=f"volver_comp_{cid}", use_container_width=True):
            st.rerun()


@st.dialog("Eliminar opción")
def _dialog_eliminar_componente(cid: int, nombre: str):
    st.markdown(f"¿Eliminar **{html.escape(str(nombre))}** permanentemente?  \n"
                "Esta acción no se puede deshacer.")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑 Sí, eliminar", key=f"confirm_eliminar_comp_{cid}", type="primary",
                     use_container_width=True):
            eliminar_componente(cid)
            st.rerun()
    with c2:
        if st.button("Volver", key=f"volver_del_comp_{cid}", use_container_width=True):
            st.rerun()


@st.dialog("Eliminar plato")
def _dialog_eliminar_item(item_id: int, nombre: str, categoria: str):
    st.markdown(f"¿Eliminar **{html.escape(str(nombre))}** permanentemente?  \n"
                "Esta acción no se puede deshacer.")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑 Sí, eliminar", key=f"confirm_eliminar_{categoria}_{item_id}",
                     type="primary", use_container_width=True):
            eliminar_item(item_id)
            if st.session_state.get(f"ed_{categoria}_id") == item_id:
                _clear_edit(categoria)
            st.rerun()
    with c2:
        if st.button("Volver", key=f"volver_del_{categoria}_{item_id}", use_container_width=True):
            st.rerun()


def _stock_btn_label(stock) -> str:
    """Etiqueta del botón-indicador de stock: '📦 N' (con control) o '♾️' (ilimitado)."""
    s = stock_int(stock)
    return "♾️" if s is None else f"📦 {s}"


@st.dialog("📦 Stock del día")
def _dialog_stock(scope: str, oid: int, nombre: str, stock_actual):
    """Editor de stock por ítem, abierto desde su tarjeta (sustituye al viejo 86). El stock
    sustituye al 86: poner 0 = agotado (se atenúa en el menú y se oculta/bloquea al pedir);
    'Ilimitado' = sin control. scope = 'comp' (componente) | 'menu' (plato a la carta)."""
    s = stock_int(stock_actual)
    st.markdown(f"Existencias de **{html.escape(str(nombre))}**")
    ilimitado = st.checkbox("Ilimitado (sin control de stock)", value=(s is None),
                            key=f"dlgstk_unl_{scope}_{oid}")
    cantidad = st.number_input("Porciones disponibles hoy", min_value=0, step=1,
                               value=int(s) if s is not None else 0,
                               key=f"dlgstk_qty_{scope}_{oid}", disabled=ilimitado)
    st.caption("0 = agotado. Se descuenta al crear el pedido y se reintegra al cancelar "
               "antes de 'listo'. El montaje masivo de la mañana está en 📦 Inventario.")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("💾 Guardar", key=f"dlgstk_save_{scope}_{oid}", type="primary",
                     use_container_width=True):
            val = None if ilimitado else int(cantidad or 0)
            (guardar_stock_componente if scope == "comp" else guardar_stock_item)(oid, val)
            st.rerun()
    with c2:
        if st.button("Volver", key=f"dlgstk_back_{scope}_{oid}", use_container_width=True):
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Pestaña 1 · Plato del Día (componentes por grupo)
# ══════════════════════════════════════════════════════════════════════════════
def _render_plato_dia():
    from db import precio_plato_dia, num_acompanamientos
    st.markdown(
        f'<div style="background:#fafaf8; border:1px solid #ececec; border-radius:10px; '
        f'padding:0.8rem 1rem; font-size:0.85rem; color:#45443e; margin-bottom:1rem;">'
        f'💡 Precio plano del Plato del Día: <b>${fmt_money(precio_plato_dia())}</b> · '
        f'el cliente elige <b>{num_acompanamientos()}</b> acompañamientos. '
        f'Cambia ambos en ⚙️ Ajustes.</div>',
        unsafe_allow_html=True,
    )

    df = cargar_componentes()
    cols = st.columns(len(GRUPOS_COMPONENTE))
    for col, grupo in zip(cols, GRUPOS_COMPONENTE):
        with col:
            _render_grupo(df[df["grupo"] == grupo] if not df.empty else df, grupo)


def _render_grupo(sub, grupo: str):
    label = GRUPO_LABEL.get(grupo, grupo.capitalize())
    activos = int((sub["activo"] == True).sum()) if not sub.empty else 0
    st.markdown(f'<div class="section-title">{label} · {activos} activo(s)</div>',
                unsafe_allow_html=True)
    if grupo == "entrada":
        st.caption("Las sopas activas aparecen aquí junto a Fruta y Huevo. "
                   "Desactiva o marca 86 una sopa para ocultarla del día.")

    if sub.empty:
        st.markdown('<p style="color:#a3a39b; font-size:0.82rem;">Sin opciones.</p>',
                    unsafe_allow_html=True)
    else:
        # Disponibles primero; inactivos / agotados / 86 bajan al fondo del grupo (mismo
        # criterio que las categorías del catálogo). La columna es estrecha, así que aquí
        # solo se REORDENA, sin encabezados de sub-sección.
        disp, no = _partir_disponibles(sub)
        for row in disp + no:
            _componente_card(row, grupo)

    # Alta rápida de una opción (la clave se rota con un nonce para limpiar el input).
    nonce = st.session_state.get(f"nonce_add_{grupo}", 0)
    nuevo = st.text_input("Agregar", key=f"add_comp_{grupo}_{nonce}",
                          placeholder="Nueva opción…", label_visibility="collapsed")
    if st.button("➕ Agregar", key=f"btn_add_comp_{grupo}", use_container_width=True):
        if nuevo.strip():
            agregar_componente(grupo, nuevo)
            st.session_state[f"nonce_add_{grupo}"] = nonce + 1
            st.rerun()
        else:
            st.warning("Escribe un nombre.")


def _componente_card(row, grupo: str):
    cid = int(row["id"])
    nombre = row["nombre"]
    activo = bool(row["activo"])
    # "Agotado" se muestra por dos vías: 86 manual del día (agotado_hasta) o stock en 0.
    # El badge/atenuado refleja ambas; el botón 86/♻ (b2) solo gestiona el 86 MANUAL
    # (el ♻ solo deshace eso; el stock se ajusta en 📦 Inventario).
    agotado_dia = _agotado_hoy(row.get("agotado_hasta"))
    sin_stock = agotado_por_stock(row.get("stock"))
    card_cls = "menu-card" if (activo and not (agotado_dia or sin_stock)) else "menu-card inactivo"
    if agotado_dia:
        estado = '<span class="badge badge-agotado">🚫 Hoy</span>'
    elif sin_stock:
        estado = '<span class="badge badge-agotado">🚫 Sin stock</span>'
    elif activo:
        estado = '<span class="badge badge-activo">●</span>'
    else:
        estado = '<span class="badge badge-inactivo">○</span>'

    st.markdown(
        f'<div class="{card_cls}" style="padding:0.6rem 0.8rem; margin-bottom:0.4rem;">'
        f'<div style="display:flex; justify-content:space-between; align-items:center;">'
        f'<div class="menu-nombre" style="font-size:0.9rem;">{html.escape(str(nombre))}</div>'
        f'<div>{estado}</div></div></div>',
        unsafe_allow_html=True,
    )
    b1, b2, b3, b4 = st.columns(4)
    with b1:
        if st.button("⏸" if activo else "▶", key=f"toggle_comp_{cid}",
                     help="Desactivar" if activo else "Activar", use_container_width=True):
            toggle_componente(cid, activo)
            st.rerun()
    with b2:
        # El indicador de stock ocupa el lugar del antiguo 86; al pulsarlo se edita.
        if st.button(_stock_btn_label(row.get("stock")), key=f"stock_comp_{cid}",
                     help="Stock de hoy (toca para editar). 0 = agotado.",
                     use_container_width=True):
            _dialog_stock("comp", cid, str(nombre), row.get("stock"))
    with b3:
        if st.button("✏️", key=f"edit_comp_{cid}", help="Renombrar", use_container_width=True):
            _dialog_editar_componente(cid, str(nombre))
    with b4:
        if st.button("🗑", key=f"eliminar_comp_{cid}", help="Eliminar", use_container_width=True):
            _dialog_eliminar_componente(cid, str(nombre))


# ══════════════════════════════════════════════════════════════════════════════
# Pestañas 2–4 · Catálogo (Especiales / A la carta / Bebidas)
# ══════════════════════════════════════════════════════════════════════════════
def _enter_edit(categoria: str, row, con_precio: bool):
    # Escribimos DIRECTO en las claves de los widgets (no en defaults aparte) para
    # esquivar el gotcha de Streamlit: un widget con key ignora value= si ya hay estado.
    st.session_state[f"ed_{categoria}_id"] = int(row["id"])
    st.session_state[f"in_{categoria}_nombre"] = str(row["nombre"])
    if con_precio:
        st.session_state[f"in_{categoria}_precio"] = int(row["precio"])
    else:
        st.session_state[f"in_{categoria}_desc"] = _txt(row.get("descripcion"))


def _clear_edit(categoria: str):
    for suf in ("nombre", "precio", "desc"):
        st.session_state.pop(f"in_{categoria}_{suf}", None)
    st.session_state.pop(f"ed_{categoria}_id", None)


def _render_catalogo_tab(categoria: str, label: str, con_precio: bool):
    df = cargar_catalogo()
    sub = df[df["categoria"] == categoria].copy() if not df.empty else df

    col_lista, col_form = st.columns([2, 1])

    with col_lista:
        # El título de la categoría ya lo muestra la cabecera del acordeón (ver render()).
        if not con_precio:
            st.caption(f"Precio plano de la categoría: ${fmt_money(precio_especiales())}. "
                       "Cámbialo en ⚙️ Ajustes (se aplica a todas por igual).")

        if sub is None or sub.empty:
            st.markdown('<p style="color:#a3a39b; font-size:0.85rem;">No hay platos en esta sección.</p>',
                        unsafe_allow_html=True)
        else:
            # Disponibles (activo + con stock + sin 86) ARRIBA; el resto (inactivo /
            # agotado / 86) en una sección aparte ABAJO. Así se ve de inmediato lo que se
            # puede vender, sin filtrar visualmente lo no disponible.
            disp, no = _partir_disponibles(sub)
            st.markdown(_subseccion(f"● Disponibles · {len(disp)}", "#15803d"),
                        unsafe_allow_html=True)
            if disp:
                for row in disp:
                    _item_card(row, categoria, con_precio)
            else:
                st.markdown('<p style="color:#a3a39b; font-size:0.82rem;">Ninguno disponible ahora.</p>',
                            unsafe_allow_html=True)
            if no:
                st.markdown(_subseccion(f"○ No disponibles · {len(no)}", "#a3a39b"),
                            unsafe_allow_html=True)
                for row in no:
                    _item_card(row, categoria, con_precio)

    with col_form:
        _item_form(categoria, label, con_precio)


def _item_card(row, categoria: str, con_precio: bool):
    iid = int(row["id"])
    nombre = row["nombre"]
    precio = int(row["precio"])
    activo = bool(row["activo"])
    # Agotado por 86 manual (agotado_hasta) o por stock en 0. El badge/atenuado refleja
    # ambas; el botón 86/♻ (b2) gestiona solo el 86 manual (el stock va en 📦 Inventario).
    agotado_dia = _agotado_hoy(row.get("agotado_hasta"))
    sin_stock = agotado_por_stock(row.get("stock"))
    desc = _txt(row.get("descripcion"))
    card_cls = "menu-card" if (activo and not (agotado_dia or sin_stock)) else "menu-card inactivo"
    badge = ('<span class="badge badge-activo">● Activo</span>' if activo
             else '<span class="badge badge-inactivo">○ Inactivo</span>')
    if agotado_dia:
        badge += ' <span class="badge badge-agotado">🚫 Hoy</span>'
    elif sin_stock:
        badge += ' <span class="badge badge-agotado">🚫 Sin stock</span>'

    if con_precio:
        sub_html = f'<div class="menu-precio">${fmt_money(precio)}</div>'
    elif desc:
        sub_html = (f'<div class="menu-precio" style="font-style:italic;">'
                    f'{html.escape(desc)}</div>')
    else:
        sub_html = ('<div class="menu-precio" style="color:#d97706;">Sin descripción</div>')

    col_card, col_btns = st.columns([3, 1.4])
    with col_card:
        st.markdown(
            f'<div class="{card_cls}">'
            f'<div style="display:flex; justify-content:space-between; align-items:center;">'
            f'<div><div class="menu-nombre">{html.escape(str(nombre))}</div>{sub_html}</div>'
            f'<div style="text-align:right;">{badge}</div></div></div>',
            unsafe_allow_html=True,
        )
    with col_btns:
        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
        b1, b2, b3, b4 = st.columns(4)
        with b1:
            if st.button("⏸" if activo else "▶", key=f"toggle_{categoria}_{iid}",
                         help="Desactivar" if activo else "Activar", use_container_width=True):
                toggle_item(iid, activo)
                st.rerun()
        with b2:
            # Indicador de stock en el lugar del antiguo 86; al pulsarlo se edita.
            if st.button(_stock_btn_label(row.get("stock")), key=f"stock_{categoria}_{iid}",
                         help="Stock de hoy (toca para editar). 0 = agotado.",
                         use_container_width=True):
                _dialog_stock("menu", iid, str(nombre), row.get("stock"))
        with b3:
            if st.button("✏️", key=f"edit_{categoria}_{iid}", help="Editar",
                         use_container_width=True):
                _enter_edit(categoria, row, con_precio)
                st.rerun()
        with b4:
            if st.button("🗑", key=f"eliminar_{categoria}_{iid}", help="Eliminar",
                         use_container_width=True):
                _dialog_eliminar_item(iid, str(nombre), categoria)


def _item_form(categoria: str, label: str, con_precio: bool):
    editando = f"ed_{categoria}_id" in st.session_state
    st.markdown(f'<div class="section-title">{"Editar" if editando else "Agregar"}</div>',
                unsafe_allow_html=True)

    nombre = st.text_input("Nombre del plato", key=f"in_{categoria}_nombre")

    precio_val = 0
    if con_precio:
        precio_val = st.number_input("Precio", min_value=0, step=1000,
                                     key=f"in_{categoria}_precio")
    else:
        st.caption(f"Precio plano: ${fmt_money(precio_especiales())} (editable en Ajustes).")

    desc = None
    if not con_precio:
        desc = st.text_area("Descripción (qué incluye)", key=f"in_{categoria}_desc",
                            placeholder="Ej: incluye todos los acompañamientos")

    b_guardar, b_cancelar = st.columns(2)
    with b_guardar:
        if st.button("💾 Guardar", type="primary", key=f"btn_guardar_{categoria}",
                     use_container_width=True):
            if not (nombre or "").strip():
                st.error("El nombre no puede estar vacío.")
            elif con_precio and int(precio_val or 0) <= 0:
                st.error("El precio debe ser mayor a 0.")
            else:
                if editando:
                    actualizar_item(
                        st.session_state[f"ed_{categoria}_id"], nombre,
                        (precio_val if con_precio else precio_especiales()), desc,
                    )
                    _clear_edit(categoria)
                else:
                    agregar_item(categoria, nombre, precio_val, desc)
                    _clear_edit(categoria)
                st.rerun()
    with b_cancelar:
        if editando:
            if st.button("✕ Cancelar", key=f"btn_cancelar_{categoria}", use_container_width=True):
                _clear_edit(categoria)
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Pestaña 5 · Ajustes (precios planos + recargo de entrega)
# ══════════════════════════════════════════════════════════════════════════════
def _int_aj(aj: dict, clave: str, default: int) -> int:
    try:
        return int(float(aj.get(clave, default)))
    except (TypeError, ValueError):
        return default


def _render_ajustes():
    aj = cargar_ajustes()
    st.markdown('<div class="section-title">Precios y recargos</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        pd_precio = st.number_input("Precio Plato del Día", min_value=0, step=1000,
                                    value=_int_aj(aj, "plato_dia_precio", 0), key="aj_plato_dia")
        esp_precio = st.number_input("Precio Especiales (plano)", min_value=0, step=1000,
                                     value=_int_aj(aj, "especiales_precio", 0), key="aj_especiales")
    with c2:
        fee = st.number_input("Recargo de entrega (Domicilio / Para Llevar)", min_value=0,
                              step=500, value=_int_aj(aj, "fee_entrega", 0), key="aj_fee")
        n_ac = st.number_input("Acompañamientos a elegir", min_value=1, max_value=6, step=1,
                               value=_int_aj(aj, "acompanamientos_n", 3), key="aj_n_ac")

    if st.button("💾 Guardar ajustes", type="primary", key="btn_guardar_ajustes"):
        guardar_ajustes({
            "plato_dia_precio": int(pd_precio),
            "especiales_precio": int(esp_precio),
            "fee_entrega": int(fee),
            "acompanamientos_n": int(n_ac),
        })
        st.rerun()

    st.markdown(
        '<div style="background:#fafaf8; border:1px solid #ececec; border-radius:10px; '
        'padding:1rem; font-size:0.78rem; color:#6b6b64; margin-top:1rem;">'
        '<div style="color:#45443e; font-weight:600; margin-bottom:6px;">💡 Cómo se aplican</div>'
        'El <b>Plato del Día</b> cuesta lo mismo sin importar la combinación elegida.<br>'
        'Las <b>Especiales</b> comparten un único precio: al cambiarlo, se actualizan todas.<br>'
        'El <b>recargo de entrega</b> se suma una vez a cada pedido de Domicilio o Para Llevar.'
        '</div>',
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Pestaña 6 · Inventario del día (stock por componente y por plato)
# ══════════════════════════════════════════════════════════════════════════════
# Pantalla de montaje diario: cada mañana el administrador fija cuántas porciones hay
# de cada micro-componente del Plato del Día (cada sopa, proteína, acompañamiento…) y
# cuántas unidades de cada plato a la carta / especial / bebida. Al guardar se inicializan
# o sobrescriben los contadores en vivo. Marcar "Ilimitado" deja el ítem SIN control
# (no se descuenta ni se oculta). Va en un st.form para no recargar en cada tecla.
def _actual_txt(stock) -> str:
    """Texto del stock vivo de una fila ('actual: N' / 'actual: ilimitado')."""
    s = stock_int(stock)
    return "actual: ilimitado" if s is None else f"actual: {s}"


def _fila_inventario(scope: str, oid: int, nombre, stock_actual):
    """Una fila del formulario: nombre + 'Ilimitado' + 'Cantidad'. Devuelve el par
    (ilimitado, cantidad) tal como quedan los widgets (se lee en el submit)."""
    s = stock_int(stock_actual)
    c_n, c_u, c_q = st.columns([3, 1.4, 1.6])
    with c_n:
        st.markdown(
            f'<div style="padding:6px 0;"><span style="font-size:0.9rem; color:#26262b;">'
            f'{html.escape(str(nombre))}</span>'
            f'<div style="font-size:0.72rem; color:#a3a39b;">{_actual_txt(stock_actual)}</div></div>',
            unsafe_allow_html=True,
        )
    with c_u:
        ilimitado = st.checkbox("Ilimitado", value=(s is None),
                                key=f"inv_{scope}_unl_{oid}")
    with c_q:
        cantidad = st.number_input("Cantidad", min_value=0, step=1,
                                   value=int(s) if s is not None else 0,
                                   key=f"inv_{scope}_qty_{oid}", label_visibility="collapsed")
    return ilimitado, int(cantidad or 0)


def _resumen_inv(sub) -> str:
    """Resumen para el título de un acordeón (se ve sin desplegar): cuántas opciones
    llevan control y cuántas están agotadas. Ej: '2/3 con control · 1 agotado'."""
    total = len(sub)
    con = sum(1 for _, r in sub.iterrows() if stock_int(r.get("stock")) is not None)
    agot = sum(1 for _, r in sub.iterrows() if agotado_por_stock(r.get("stock")))
    txt = f"{con}/{total} con control"
    if agot:
        txt += f" · {agot} agotado"
    return txt


def _render_inventario():
    st.markdown(
        '<div style="background:#fafaf8; border:1px solid #ececec; border-radius:10px; '
        'padding:0.8rem 1rem; font-size:0.85rem; color:#45443e; margin-bottom:1rem;">'
        '📦 Fija el <b>stock del día</b>. Cada componente del Plato del Día se cuenta por '
        'separado (50 sopas, 50 ensaladas, 50 res, 50 pollo…) y cada plato a la carta como '
        'una unidad. Al guardar se <b>sobrescriben</b> los contadores en vivo. Marca '
        '<b>Ilimitado</b> para no controlar un ítem.</div>',
        unsafe_allow_html=True,
    )

    df_comp = cargar_componentes()
    df_cat = cargar_catalogo()
    comp_vals, menu_vals = {}, {}

    with st.form("form_inventario_dia"):
        st.markdown(titulo_seccion('🍛 Plato del Día · por componente'),
                    unsafe_allow_html=True)
        if df_comp is None or df_comp.empty:
            st.caption("No hay componentes del Plato del Día. Créalos en la pestaña 🍽️ Plato del Día.")
        else:
            # Un acordeón por grupo (colapsado): mantiene la pantalla compacta y solo se
            # despliega hacia abajo el grupo que el admin toca. El título lleva el resumen.
            for grupo in GRUPOS_COMPONENTE:
                sub = df_comp[df_comp["grupo"] == grupo]
                if sub.empty:
                    continue
                with st.expander(f"{GRUPO_LABEL.get(grupo, grupo)} · {_resumen_inv(sub)}",
                                 expanded=False):
                    for _, row in sub.iterrows():
                        cid = int(row["id"])
                        comp_vals[cid] = _fila_inventario("comp", cid, row["nombre"], row.get("stock"))

        st.markdown(titulo_seccion('🍽️ Platos a la carta · por unidad', style="margin-top:1rem;"),
                    unsafe_allow_html=True)
        hay_cat = False
        for categoria, label in [("especial", "⭐ Especiales"),
                                 ("a_la_carta", "📋 A la carta"),
                                 ("bebida", "🥤 Bebidas")]:
            sub = df_cat[df_cat["categoria"] == categoria] if not df_cat.empty else df_cat
            if sub is None or sub.empty:
                continue
            hay_cat = True
            with st.expander(f"{label} · {_resumen_inv(sub)}", expanded=False):
                for _, row in sub.iterrows():
                    mid = int(row["id"])
                    menu_vals[mid] = _fila_inventario("menu", mid, row["nombre"], row.get("stock"))
        if not hay_cat:
            st.caption("No hay platos a la carta. Agrégalos en sus pestañas.")

        st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)
        guardado = st.form_submit_button("📦 Guardar inventario del día", type="primary",
                                         use_container_width=True)

    if guardado:
        comp_stock = {cid: (None if unl else qty) for cid, (unl, qty) in comp_vals.items()}
        menu_stock = {mid: (None if unl else qty) for mid, (unl, qty) in menu_vals.items()}
        guardar_inventario(comp_stock, menu_stock)
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Pestaña 7 · Importar menú desde Excel / CSV (con fusión al inventario)
# ══════════════════════════════════════════════════════════════════════════════
# Sube un .xlsx/.csv con una fila por ítem. La columna 'seccion' enruta cada fila a su
# panel (componentes del Plato del Día vs catálogo a la carta/especiales/bebidas) y se
# mapean nombre/descr/precio/stock de una sola vez. El upsert es NO destructivo: una
# celda de stock en blanco NUNCA toca el contador (un re-import de solo precios conserva
# el inventario), no duplica filas (identidad por nombre dentro de su sección) y no rompe
# el seguimiento en curso. Acceso: admin + caja (capacidad edit_menu).

# seccion (normalizada) → (clase, destino). 'comp' = menu_componentes.grupo;
# 'menu' = menu.categoria.
SECCION_MAP = {
    "entrada": ("comp", "entrada"), "entradas": ("comp", "entrada"),
    "principio": ("comp", "principio"), "principios": ("comp", "principio"),
    "proteina": ("comp", "proteina"), "proteinas": ("comp", "proteina"),
    "carne": ("comp", "proteina"), "carnes": ("comp", "proteina"),
    "acompanamiento": ("comp", "acompanamiento"), "acompanamientos": ("comp", "acompanamiento"),
    "acomp": ("comp", "acompanamiento"), "guarnicion": ("comp", "acompanamiento"),
    "especial": ("menu", "especial"), "especiales": ("menu", "especial"),
    "a la carta": ("menu", "a_la_carta"), "a_la_carta": ("menu", "a_la_carta"),
    "carta": ("menu", "a_la_carta"), "plato": ("menu", "a_la_carta"),
    "plato a la carta": ("menu", "a_la_carta"), "fuerte": ("menu", "a_la_carta"),
    "adicional": ("menu", "adicional"), "adicionales": ("menu", "adicional"),
    "extra": ("menu", "adicional"), "extras": ("menu", "adicional"),
    "bebida": ("menu", "bebida"), "bebidas": ("menu", "bebida"),
    "jugo": ("menu", "bebida"), "gaseosa": ("menu", "bebida"), "drink": ("menu", "bebida"),
}
# Alias aceptados por columna (normalizados: sin acentos, en minúscula).
COL_ALIAS = {
    "seccion":     ("seccion", "categoria", "tipo", "grupo", "seccion/categoria"),
    "nombre":      ("nombre", "name", "plato", "producto", "item", "articulo"),
    "descripcion": ("descripcion", "desc", "detalle"),
    "precio":      ("precio", "price", "valor", "costo"),
    "stock":       ("stock", "cantidad", "existencias", "inventario", "disponibles",
                    "cantidad disponible"),
    "activo":      ("activo", "active", "disponible", "habilitado"),
}
DEST_LABEL = {
    "entrada": "Entrada", "principio": "Principio", "proteina": "Proteína",
    "acompanamiento": "Acompañamientos", "especial": "Especiales",
    "a_la_carta": "A la carta", "adicional": "Adicionales", "bebida": "Bebidas",
}


def _norm(s) -> str:
    """minúsculas + sin acentos + sin espacios extremos, para casar encabezados/secciones."""
    s = str("" if s is None else s).strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _parse_int(v):
    """int >= 0 desde celda numérica o texto ('25.000'/'$25,000' → 25000); None si está
    en blanco. Distingue número (uso directo) de texto (quita separadores de miles)."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, bool):
        return None
    if isinstance(v, numbers.Number):
        try:
            return max(0, int(round(float(v))))
        except (TypeError, ValueError):
            return None
    s = str(v).strip().replace("$", "").replace(" ", "")
    if s == "":
        return None
    s = s.replace(".", "").replace(",", "")
    try:
        return max(0, int(s))
    except ValueError:
        return None


def _parse_activo(v) -> bool:
    """Activo por defecto; solo 'no/false/0/inactivo/n/off' lo desactivan."""
    try:
        if v is None or pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return _norm(v) not in ("no", "false", "0", "inactivo", "n", "off")


def _celda_texto(v):
    """str limpia o None (para descripción)."""
    try:
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s or None


def _mapear_columnas(df) -> dict:
    """{rol: nombre_real_de_columna} resolviendo alias por encabezado normalizado."""
    norm_cols = {_norm(c): c for c in df.columns}
    out = {}
    for rol, alias in COL_ALIAS.items():
        for a in alias:
            if a in norm_cols:
                out[rol] = norm_cols[a]
                break
    return out


def _parse_filas(df):
    """(filas_validas, errores). Cada fila: {kind, destino, nombre, descripcion, precio,
    stock, activo, fila}. Salta filas sin nombre; reporta secciones desconocidas."""
    cols = _mapear_columnas(df)
    if "seccion" not in cols or "nombre" not in cols:
        return [], ["Faltan columnas obligatorias: 'seccion' y 'nombre'. "
                    "Descarga la plantilla para ver el formato."]
    filas, errores = [], []
    for i, row in df.iterrows():
        nfila = int(i) + 2  # +2: fila de encabezado + base 1
        nombre = _celda_texto(row[cols["nombre"]])
        if not nombre:
            continue  # fila vacía → se ignora en silencio
        sec_raw = row[cols["seccion"]]
        sec = _norm(sec_raw)
        if sec not in SECCION_MAP:
            errores.append(f"Fila {nfila}: sección desconocida «{_celda_texto(sec_raw) or ''}» "
                           f"(ítem «{nombre}»).")
            continue
        kind, destino = SECCION_MAP[sec]
        filas.append({
            "kind": kind, "destino": destino, "nombre": nombre,
            "descripcion": _celda_texto(row[cols["descripcion"]]) if "descripcion" in cols else None,
            "precio": _parse_int(row[cols["precio"]]) if "precio" in cols else None,
            "stock":  _parse_int(row[cols["stock"]]) if "stock" in cols else None,
            "activo": _parse_activo(row[cols["activo"]]) if "activo" in cols else True,
            "fila": nfila,
        })
    return filas, errores


def _leer_dataframe(archivo):
    """Lee el archivo subido a DataFrame. .csv nativo (con fallback latin-1); .xlsx vía
    openpyxl (ImportError si falta la librería → lo maneja la vista)."""
    nombre = (getattr(archivo, "name", "") or "").lower()
    if nombre.endswith(".csv"):
        try:
            return pd.read_csv(archivo, dtype=str)
        except UnicodeDecodeError:
            archivo.seek(0)
            return pd.read_csv(archivo, dtype=str, encoding="latin-1")
    return pd.read_excel(archivo)


def _calc_stock(actual, valor, sumar: bool, existe: bool):
    """Stock resultante de un upsert:
      · valor None (celda en blanco) → NO tocar (deja el actual; NULL/ilimitado si es nuevo).
      · sumar=False (sobrescribir)   → valor (inicializa el contador).
      · sumar=True  (reabastecer)    → actual + valor para los existentes con contador; el
                                       valor tal cual para nuevos o los que estaban ilimitados.
    Nunca 'borra' un contador: una celda en blanco preserva el estado vivo del inventario."""
    if valor is None:
        return actual if existe else None
    if sumar and existe and actual is not None:
        return int(actual) + int(valor)
    return int(valor)


def importar_menu(filas, modo_stock: str = "sobrescribir") -> dict:
    """Aplica el import en UNA transacción (atómico). Upsert por identidad:
    componentes por (grupo, LOWER(nombre)); catálogo por (categoria, LOWER(nombre)).
    No duplica filas ni rompe el inventario; el stock se fusiona según _calc_stock.
    Devuelve un resumen con conteos y omitidos."""
    res = {"comp_nuevos": 0, "comp_act": 0, "menu_nuevos": 0, "menu_act": 0,
           "omitidos": []}
    sumar = (modo_stock == "sumar")
    with engine.begin() as conn:
        for f in filas:
            nombre = f["nombre"]
            if f["kind"] == "comp":
                grupo = f["destino"]
                ex = conn.execute(text(
                    "SELECT id, stock FROM menu_componentes "
                    "WHERE grupo = :g AND LOWER(nombre) = LOWER(:n)"
                ), {"g": grupo, "n": nombre}).mappings().first()
                nuevo_stock = _calc_stock(ex["stock"] if ex else None, f["stock"], sumar, bool(ex))
                if ex:
                    conn.execute(text(
                        "UPDATE menu_componentes SET activo = :a, stock = :s WHERE id = :id"
                    ), {"a": f["activo"], "s": nuevo_stock, "id": ex["id"]})
                    res["comp_act"] += 1
                else:
                    mx = conn.execute(text(
                        "SELECT COALESCE(MAX(orden), 0) FROM menu_componentes WHERE grupo = :g"
                    ), {"g": grupo}).scalar()
                    conn.execute(text(
                        "INSERT INTO menu_componentes (grupo, nombre, activo, orden, stock) "
                        "VALUES (:g, :n, :a, :o, :s)"
                    ), {"g": grupo, "n": nombre, "a": f["activo"], "o": int(mx) + 1, "s": nuevo_stock})
                    res["comp_nuevos"] += 1
            else:
                categoria = f["destino"]
                ex = conn.execute(text(
                    "SELECT id, stock FROM menu WHERE categoria = :c AND LOWER(nombre) = LOWER(:n)"
                ), {"c": categoria, "n": nombre}).mappings().first()
                # Especiales: precio plano de la categoría; el resto usa el del archivo.
                precio = precio_especiales() if categoria == "especial" else f["precio"]
                if not ex and categoria != "especial" and (precio is None or precio <= 0):
                    res["omitidos"].append(
                        f"Fila {f['fila']}: «{nombre}» sin precio válido (requerido para "
                        f"un plato/bebida nuevo).")
                    continue
                nuevo_stock = _calc_stock(ex["stock"] if ex else None, f["stock"], sumar, bool(ex))
                if ex:
                    sets = ["activo = :a", "stock = :s"]
                    params = {"a": f["activo"], "s": nuevo_stock, "id": ex["id"]}
                    if categoria == "especial":
                        sets.append("precio = :p"); params["p"] = int(precio)
                    elif precio is not None:           # precio en blanco → conserva el actual
                        sets.append("precio = :p"); params["p"] = int(precio)
                    if f["descripcion"] is not None:   # descr en blanco → conserva la actual
                        sets.append("descripcion = :d"); params["d"] = f["descripcion"]
                    conn.execute(text(f"UPDATE menu SET {', '.join(sets)} WHERE id = :id"), params)
                    res["menu_act"] += 1
                else:
                    mx = conn.execute(text(
                        "SELECT COALESCE(MAX(orden), 0) FROM menu WHERE categoria = :c"
                    ), {"c": categoria}).scalar()
                    conn.execute(text(
                        "INSERT INTO menu (nombre, precio, activo, orden, categoria, descripcion, stock) "
                        "VALUES (:n, :p, :a, :o, :c, :d, :s)"
                    ), {"n": nombre, "p": int(precio or 0), "a": f["activo"], "o": int(mx) + 1,
                        "c": categoria, "d": f["descripcion"], "s": nuevo_stock})
                    res["menu_nuevos"] += 1
    _clear_menu_caches()
    cargar_componentes.clear()
    return res


def _plantilla_csv() -> bytes:
    """Plantilla de ejemplo descargable (utf-8-sig para que Excel abra los acentos)."""
    filas = [
        ["seccion", "nombre", "descripcion", "precio", "stock", "activo"],
        ["entrada", "Sopa del día", "", "", "50", "si"],
        ["principio", "Frijol", "", "", "40", "si"],
        ["proteina", "Pollo", "", "", "40", "si"],
        ["acompanamiento", "Arroz", "", "", "100", "si"],
        ["especial", "Bandeja Paisa", "Frijol, arroz, carne, chicharrón, huevo", "", "20", "si"],
        ["a_la_carta", "Hamburguesa clásica", "", "25000", "30", "si"],
        ["bebida", "Limonada natural", "", "5000", "", "si"],
    ]
    buf = io.StringIO()
    csv.writer(buf).writerows(filas)
    return buf.getvalue().encode("utf-8-sig")


# Opciones del desplegable de 'seccion' (texto amigable; el parser las normaliza a las
# claves de SECCION_MAP: «Proteína»→proteina, «A la carta»→a la carta, etc.).
PLANTILLA_SECCIONES = ["Entrada", "Principio", "Proteína", "Acompañamiento",
                       "Especiales", "A la carta", "Bebidas"]
# Ejemplos (uno por sección) que se precargan en la hoja, en estilo tenue para que el
# cliente los reemplace. [Sección, Nombre, Descripción, Precio, Stock, Activo].
PLANTILLA_EJEMPLOS = [
    ["Entrada", "Sopa del día", None, None, 50, "Sí"],
    ["Principio", "Frijol", None, None, 40, "Sí"],
    ["Proteína", "Pollo a la plancha", None, None, 40, "Sí"],
    ["Acompañamiento", "Arroz blanco", None, None, 100, "Sí"],
    ["Especiales", "Bandeja Paisa", "Frijol, arroz, carne, chicharrón, huevo, plátano",
     None, 20, "Sí"],
    ["A la carta", "Hamburguesa clásica", "Carne 150g, queso y papas", 25000, 30, "Sí"],
    ["Bebidas", "Limonada natural", "Vaso 16oz", 5000, None, "Sí"],
]


def _plantilla_xlsx() -> bytes:
    """Plantilla Excel rica: encabezados con estilo, desplegables (Sección / Activo),
    comentarios de ayuda, formato de precio/stock, ejemplos por sección y una hoja de
    Instrucciones. Mantiene las columnas que el importador entiende. Requiere openpyxl."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.comments import Comment
    from openpyxl.utils import get_column_letter

    AZUL, AZUL_CLARO, GRIS, GRIS_TXT = "1E293B", "F1F5F9", "F8FAFC", "64748B"
    wb = Workbook()

    # ── Hoja 1 · Menú (la que llena el cliente) ─────────────────────────────────
    ws = wb.active
    ws.title = "Menú"
    encabezados = ["Sección", "Nombre", "Descripción", "Precio", "Stock", "Activo"]
    ws.append(encabezados)

    fill_h = PatternFill("solid", fgColor=AZUL)
    font_h = Font(bold=True, color="FFFFFF", size=11)
    center = Alignment(horizontal="center", vertical="center")
    borde = Border(bottom=Side(style="thin", color="CBD5E1"))
    for col in range(1, len(encabezados) + 1):
        c = ws.cell(row=1, column=col)
        c.fill, c.font, c.alignment, c.border = fill_h, font_h, center, borde
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = "A2"
    for i, w in enumerate([16, 30, 44, 12, 10, 10], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ayudas = {
        1: ("Sección del menú (elige de la lista). Entrada · Principio · Proteína · "
            "Acompañamiento van al PLATO DEL DÍA. Especiales · A la carta · Bebidas van a la carta."),
        2: "Nombre del ítem. Si ya existe en esa sección, se ACTUALIZA (no se duplica).",
        3: "Opcional. Lo que incluye (sobre todo útil en Especiales).",
        4: ("Precio en pesos, solo el número (ej: 25000). A la carta y Bebidas: obligatorio "
            "si es nuevo. Especiales usan el precio plano de Ajustes. Componentes: sin precio."),
        5: "Cantidad de hoy. VACÍO = ilimitado / no cambia el contador. 0 = agotado.",
        6: "Sí = se ofrece · No = inactivo.",
    }
    for col, txt in ayudas.items():
        cm = Comment(txt, "Plantilla")
        cm.width, cm.height = 280, 130
        ws.cell(row=1, column=col).comment = cm

    # Ejemplos en estilo tenue (el cliente los reemplaza).
    font_ej = Font(italic=True, color=GRIS_TXT)
    fill_ej = PatternFill("solid", fgColor=GRIS)
    for fila in PLANTILLA_EJEMPLOS:
        ws.append(fila)
    n_ej = len(PLANTILLA_EJEMPLOS)
    for row in range(2, 2 + n_ej):
        for col in range(1, 7):
            ws.cell(row=row, column=col).font = font_ej
            ws.cell(row=row, column=col).fill = fill_ej
    nota = Comment("Filas de ejemplo: edítalas o bórralas y escribe tu propio menú.", "Plantilla")
    nota.width, nota.height = 260, 80
    ws.cell(row=2, column=2).comment = nota

    # Formato numérico + relleno alterno para muchas filas vacías listas para llenar.
    ultima = 400
    for row in range(2, ultima + 1):
        ws.cell(row=row, column=4).number_format = "#,##0"
        ws.cell(row=row, column=5).number_format = "0"

    # Desplegables (validación de datos) en Sección y Activo.
    dv_sec = DataValidation(type="list",
                            formula1='"' + ",".join(PLANTILLA_SECCIONES) + '"',
                            allow_blank=True)
    dv_sec.errorTitle, dv_sec.error = "Sección inválida", "Elige una sección de la lista."
    dv_sec.promptTitle, dv_sec.prompt = "Sección", "Elige a qué panel va este ítem."
    ws.add_data_validation(dv_sec)
    dv_sec.add(f"A2:A{ultima}")

    dv_act = DataValidation(type="list", formula1='"Sí,No"', allow_blank=True)
    ws.add_data_validation(dv_act)
    dv_act.add(f"F2:F{ultima}")

    # ── Hoja 2 · Instrucciones ──────────────────────────────────────────────────
    ws2 = wb.create_sheet("Instrucciones")
    ws2.column_dimensions["A"].width = 115
    wrap = Alignment(wrap_text=True, vertical="top")
    f_title = Font(bold=True, size=16, color=AZUL)
    f_h = Font(bold=True, size=12, color=AZUL)
    f_p = Font(size=11, color="334155")
    lineas = [
        ("t", "📥 Cómo llenar la plantilla del menú"),
        ("p", "Llena la hoja «Menú»: una fila por cada plato, ingrediente o bebida. "
              "Al subirla verás una vista previa antes de guardar."),
        ("h", "Sección (obligatoria · lista desplegable)"),
        ("p", "• Entrada / Principio / Proteína / Acompañamiento → componentes del Plato del Día."),
        ("p", "• Especiales → platos especiales (precio plano definido en Ajustes)."),
        ("p", "• A la carta → platos sueltos con su propio precio."),
        ("p", "• Bebidas → bebidas con su propio precio."),
        ("h", "Nombre (obligatorio)"),
        ("p", "Si el nombre ya existe en esa sección, se ACTUALIZA; no se crea un duplicado."),
        ("h", "Descripción (opcional)"),
        ("p", "Lo que incluye el plato. Recomendado en Especiales."),
        ("h", "Precio"),
        ("p", "Solo el número, en pesos (ej: 25000). Obligatorio para A la carta y Bebidas "
              "nuevas. Especiales lo ignoran (usan el precio plano). Componentes no llevan precio."),
        ("h", "Stock (inventario del día)"),
        ("p", "• Un número = porciones/unidades disponibles hoy."),
        ("p", "• Vacío = ilimitado y NO cambia el contador actual (ideal para actualizar solo precios)."),
        ("p", "• 0 = agotado."),
        ("h", "Activo"),
        ("p", "Sí = se ofrece · No = queda inactivo."),
        ("h", "Al subir"),
        ("p", "• «Sobrescribir» fija el stock al número del archivo · «Sumar» lo añade al actual."),
        ("p", "• Una celda de stock vacía nunca borra el inventario en curso."),
        ("p", "• Borra o reemplaza las filas de ejemplo de la hoja «Menú»."),
    ]
    for r, (kind, txt) in enumerate(lineas, start=1):
        c = ws2.cell(row=r, column=1, value=txt)
        c.alignment = wrap
        c.font = f_title if kind == "t" else (f_h if kind == "h" else f_p)
        if kind == "t":
            ws2.row_dimensions[r].height = 26

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _render_importar():
    # Resultado del import previo (sobrevive al reset del uploader): se muestra y se limpia.
    prev = st.session_state.pop("import_resultado", None)
    if prev:
        nuevos = prev["comp_nuevos"] + prev["menu_nuevos"]
        act = prev["comp_act"] + prev["menu_act"]
        st.success(f"✅ Importación completada · {nuevos} ítem(s) nuevo(s) · {act} actualizado(s).")
        if prev["omitidos"]:
            with st.expander(f"⚠️ {len(prev['omitidos'])} fila(s) omitida(s)"):
                for o in prev["omitidos"]:
                    st.markdown(f"- {html.escape(o)}")

    st.markdown(
        '<div style="background:#fafaf8; border:1px solid #ececec; border-radius:10px; '
        'padding:0.8rem 1rem; font-size:0.85rem; color:#45443e; margin-bottom:1rem;">'
        '📥 Sube el menú en <b>.xlsx</b> o <b>.csv</b>, una fila por ítem. La columna '
        '<b>seccion</b> enruta cada fila a su panel y se cargan nombre, descripción, precio '
        'y stock de una vez. Si una fila ya existe (mismo nombre en su sección) se '
        '<b>actualiza</b> sin duplicar; una celda de <b>stock en blanco no cambia</b> el '
        'inventario en curso.<br><span style="color:#6b6b64;">Secciones válidas: entrada · '
        'principio · proteina · acompanamiento · especial · a_la_carta · bebida.</span></div>',
        unsafe_allow_html=True,
    )

    # Plantilla rica en Excel (desplegables + ejemplos + hoja de instrucciones); .csv simple
    # como respaldo (y por si el servidor aún no tiene openpyxl).
    dcol1, dcol2 = st.columns(2)
    with dcol1:
        try:
            st.download_button(
                "⬇️ Descargar plantilla Excel", data=_plantilla_xlsx(),
                file_name="plantilla_menu.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, type="primary",
                help="Excel guiado: listas desplegables, ejemplos y hoja de instrucciones.")
        except Exception:
            st.caption("Plantilla Excel no disponible en este servidor; usa la versión .csv →")
    with dcol2:
        st.download_button(
            "⬇️ Plantilla simple (.csv)", data=_plantilla_csv(),
            file_name="plantilla_menu.csv", mime="text/csv", use_container_width=True,
            help="Misma estructura en texto plano, por si prefieres CSV.")

    nonce = int(st.session_state.get("import_nonce", 0))
    archivo = st.file_uploader("Archivo del menú (.xlsx o .csv)", type=["xlsx", "csv"],
                               key=f"import_file_{nonce}")
    if archivo is None:
        return

    try:
        df = _leer_dataframe(archivo)
    except ImportError:
        st.error("No se pudo leer el .xlsx (falta la librería openpyxl en el servidor). "
                 "Mientras tanto, sube el menú en formato .csv.")
        return
    except Exception as e:  # archivo corrupto / formato inesperado
        st.error(f"No se pudo leer el archivo: {html.escape(str(e))}")
        return

    if df is None or df.empty:
        st.warning("El archivo no tiene filas.")
        return

    filas, errores = _parse_filas(df)

    st.markdown('<div class="section-title">Vista previa</div>', unsafe_allow_html=True)
    st.dataframe(df, use_container_width=True, height=240)

    if errores:
        st.warning("Avisos:\n" + "\n".join(f"- {e}" for e in errores))

    if not filas:
        st.error("No hay filas válidas para importar. Revisa la columna 'seccion' y 'nombre'.")
        return

    # Conteo por destino (qué panel recibe qué).
    conteo = {}
    for f in filas:
        conteo[f["destino"]] = conteo.get(f["destino"], 0) + 1
    chips = " ".join(
        f'<span class="badge badge-activo">{DEST_LABEL.get(d, d)}: {n}</span>'
        for d, n in sorted(conteo.items()))
    st.markdown(f'<div style="margin:6px 0 10px;">{chips}</div>', unsafe_allow_html=True)

    st.caption("Stock al importar")
    modo = st.radio("Stock al importar", ["Sobrescribir (inicializar)", "Sumar (reabastecer)"],
                    horizontal=True, label_visibility="collapsed", key="import_modo_stock")
    modo_stock = "sumar" if modo.startswith("Sumar") else "sobrescribir"
    st.caption("Sobrescribir fija el stock al número del archivo; Sumar lo añade al actual. "
               "En ambos casos, una celda vacía deja el contador como está.")

    if st.button(f"📥 Importar {len(filas)} ítem(s)", type="primary",
                 use_container_width=True, key="btn_importar_menu"):
        res = importar_menu(filas, modo_stock)
        st.session_state["import_resultado"] = res
        st.session_state["import_nonce"] = nonce + 1   # limpia el uploader y la vista previa
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: MENÚ
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# VISTA DE SOLO LECTURA (mesero) — carta activa de hoy, sin edición
# ══════════════════════════════════════════════════════════════════════════════
def _render_readonly():
    """Carta de SOLO consulta para el mesero: secciones, platos disponibles HOY y
    precios. Sin pestañas de edición ni modales de crear/editar/eliminar/86."""
    st.markdown(titulo_seccion('🍔 Carta (solo lectura)'),
                unsafe_allow_html=True)

    def _cards(rows):
        for r in rows:
            nombre = html.escape(_txt(r.get("nombre")))
            desc = html.escape(_txt(r.get("descripcion")))
            precio = int(r.get("precio", 0) or 0)
            sin_stock = agotado_por_stock(r.get("stock"))
            card_cls = "menu-card inactivo" if sin_stock else "menu-card"
            desc_html = f'<div class="menu-precio">{desc}</div>' if desc else ""
            badge = (' <span class="badge badge-agotado">🚫 Sin stock</span>'
                     if sin_stock else "")
            st.markdown(
                f'<div class="{card_cls}">'
                f'<div style="display:flex; justify-content:space-between; align-items:center;">'
                f'<div><div class="menu-nombre">{nombre}</div>{desc_html}'
                f'<div class="menu-precio">${fmt_money(precio)}</div></div>'
                f'<div style="text-align:right;">{_stock_chip(r.get("stock"))}{badge}</div>'
                f'</div></div>',
                unsafe_allow_html=True,
            )

    # 🍽️ Plato del Día (precio plano + componentes disponibles por grupo)
    st.markdown(
        f'<div style="background:#fafaf8; border:1px solid #ececec; border-radius:10px; '
        f'padding:0.7rem 1rem; font-size:0.85rem; color:#45443e; margin:0.5rem 0 1rem 0;">'
        f'🍽️ <b>Plato del Día</b> · ${fmt_money(precio_plato_dia())} · '
        f'elige {num_acompanamientos()} acompañamientos</div>',
        unsafe_allow_html=True,
    )
    def _opt_label(o):
        """Nombre del componente con sus porciones para el mesero: 'Pollo (12)',
        ámbar si quedan pocas, tachado si está agotado, sin nada si es ilimitado."""
        s = stock_int(o.get("stock"))
        nom = html.escape(str(o["nombre"]))
        if s is None:
            return nom
        if s <= 0:
            return (f'<span style="color:#b91c1c; text-decoration:line-through;">'
                    f'{nom} (agotado)</span>')
        color = "#b45309" if s <= STOCK_BAJO else "#6b6b64"
        return f'{nom} <span style="color:{color}; font-weight:600;">({s})</span>'

    por_grupo = componentes_activos_por_grupo()
    for grupo in GRUPOS_COMPONENTE:
        opciones = por_grupo.get(grupo, [])
        if not opciones:
            continue
        nombres = " · ".join(_opt_label(o) for o in opciones)
        st.markdown(
            f'<div style="margin-bottom:0.6rem;"><span style="font-weight:600; color:#26262b;">'
            f'{html.escape(GRUPO_LABEL.get(grupo, grupo))}:</span> '
            f'<span style="color:#6b6b64; font-size:0.88rem;">{nombres}</span></div>',
            unsafe_allow_html=True,
        )

    cat = cargar_catalogo()
    disp = disponibles(cat) if not cat.empty else cat

    def _seccion(titulo: str, categoria: str):
        rows = (disp[disp["categoria"] == categoria].to_dict("records")
                if not disp.empty else [])
        st.markdown(titulo_seccion(titulo), unsafe_allow_html=True)
        if rows:
            _cards(rows)
        else:
            st.markdown('<p style="color:#a3a39b; font-size:0.85rem;">Sin platos activos.</p>',
                        unsafe_allow_html=True)

    _seccion("⭐ Especiales", "especial")
    _seccion("📋 A la carta", "a_la_carta")
    _seccion("🍟 Adicionales", "adicional")
    _seccion("🥤 Bebidas", "bebida")


def render():
    # RBAC: el mesero solo CONSULTA la carta. Bloquea todas las pestañas de edición,
    # los modales (crear/editar/eliminar) y los toggles (activo / 86).
    if not auth.can("edit_menu"):
        _render_readonly()
        return

    # Acordeón vertical (reemplaza las pestañas): cada categoría se pliega/despliega de
    # forma independiente para reducir el desplazamiento cuando la carta crece. El estado
    # de apertura vive en session_state (ver _acc_header) y sobrevive a los st.rerun() de
    # cada acción. Inventario e Importar se movieron a 💰 Caja (siguen definidos arriba y
    # se llaman desde views/caja.py); aquí el Menú se enfoca en la carta y los ajustes.
    _inject_accordion_css()
    st.session_state.setdefault("acc_open_plato_dia", True)  # la principal, abierta al entrar

    df_cat = cargar_catalogo()
    df_comp = cargar_componentes()

    # 🍽️ Plato del Día (componentes por grupo)
    n_pd = len(_partir_disponibles(df_comp)[0])
    if _acc_header("plato_dia", "🍽️ Plato del Día", f"{n_pd} opción(es) activa(s)"):
        _render_plato_dia()

    # Catálogo: Especiales / A la carta / Adicionales / Bebidas
    for cat, label, con_precio in [("especial", "⭐ Especiales", False),
                                   ("a_la_carta", "📋 A la carta", True),
                                   ("adicional", "🍟 Adicionales", True),
                                   ("bebida", "🥤 Bebidas", True)]:
        sub = (df_cat[df_cat["categoria"] == cat]
               if df_cat is not None and not df_cat.empty else df_cat)
        if _acc_header(cat, label, _resumen_cat(sub)):
            # _render_catalogo_tab espera la etiqueta SIN emoji (la del formulario).
            _render_catalogo_tab(cat, label.split(" ", 1)[1], con_precio=con_precio)

    # ⚙️ Ajustes (precios planos + recargo de entrega)
    if _acc_header("ajustes", "⚙️ Ajustes"):
        _render_ajustes()
