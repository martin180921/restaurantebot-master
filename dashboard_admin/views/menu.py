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
import pandas as pd
from datetime import date

import auth
from db import (engine, cargar_menu, cargar_componentes, cargar_catalogo,
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


# ══════════════════════════════════════════════════════════════════════════════
# Pestaña 1 · Plato del Día (componentes por grupo)
# ══════════════════════════════════════════════════════════════════════════════
def _render_plato_dia():
    from db import precio_plato_dia, num_acompanamientos
    st.markdown(
        f'<div style="background:#f9fafb; border:1px solid #e5e7eb; border-radius:10px; '
        f'padding:0.8rem 1rem; font-size:0.85rem; color:#374151; margin-bottom:1rem;">'
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
        st.markdown('<p style="color:#9ca3af; font-size:0.82rem;">Sin opciones.</p>',
                    unsafe_allow_html=True)
    else:
        for _, row in sub.iterrows():
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
        f'<div>{_stock_chip(row.get("stock"))}{estado}</div></div></div>',
        unsafe_allow_html=True,
    )
    b1, b2, b3, b4 = st.columns(4)
    with b1:
        if st.button("⏸" if activo else "▶", key=f"toggle_comp_{cid}",
                     help="Desactivar" if activo else "Activar", use_container_width=True):
            toggle_componente(cid, activo)
            st.rerun()
    with b2:
        if agotado_dia:
            if st.button("♻", key=f"unago_comp_{cid}", help="Disponible de nuevo",
                         use_container_width=True):
                quitar_agotado_componente(cid)
                st.rerun()
        else:
            if st.button("86", key=f"ago_comp_{cid}", help="Agotado hoy (vuelve mañana)",
                         use_container_width=True):
                marcar_agotado_componente(cid)
                st.rerun()
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
        st.markdown(f'<div class="section-title">{label}</div>', unsafe_allow_html=True)
        if not con_precio:
            st.caption(f"Precio plano de la categoría: ${fmt_money(precio_especiales())}. "
                       "Cámbialo en ⚙️ Ajustes (se aplica a todas por igual).")

        if sub is None or sub.empty:
            st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">No hay platos en esta sección.</p>',
                        unsafe_allow_html=True)
        else:
            for _, row in sub.iterrows():
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
            f'<div style="text-align:right;">{_stock_chip(row.get("stock"))}{badge}</div></div></div>',
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
            if agotado_dia:
                if st.button("♻", key=f"unago_{categoria}_{iid}", help="Disponible de nuevo",
                             use_container_width=True):
                    quitar_agotado_item(iid)
                    st.rerun()
            else:
                if st.button("86", key=f"ago_{categoria}_{iid}", help="Agotado hoy",
                             use_container_width=True):
                    marcar_agotado_item(iid)
                    st.rerun()
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
        '<div style="background:#f9fafb; border:1px solid #e5e7eb; border-radius:10px; '
        'padding:1rem; font-size:0.78rem; color:#6b7280; margin-top:1rem;">'
        '<div style="color:#374151; font-weight:600; margin-bottom:6px;">💡 Cómo se aplican</div>'
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
            f'<div style="padding:6px 0;"><span style="font-size:0.9rem; color:#1a1a1a;">'
            f'{html.escape(str(nombre))}</span>'
            f'<div style="font-size:0.72rem; color:#9ca3af;">{_actual_txt(stock_actual)}</div></div>',
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
        '<div style="background:#f9fafb; border:1px solid #e5e7eb; border-radius:10px; '
        'padding:0.8rem 1rem; font-size:0.85rem; color:#374151; margin-bottom:1rem;">'
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
        st.markdown('<div class="section-title">🍛 Plato del Día · por componente</div>',
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

        st.markdown('<div class="section-title" style="margin-top:1rem;">🍽️ Platos a la carta · '
                    'por unidad</div>', unsafe_allow_html=True)
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
# SECCIÓN: MENÚ
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# VISTA DE SOLO LECTURA (mesero) — carta activa de hoy, sin edición
# ══════════════════════════════════════════════════════════════════════════════
def _render_readonly():
    """Carta de SOLO consulta para el mesero: secciones, platos disponibles HOY y
    precios. Sin pestañas de edición ni modales de crear/editar/eliminar/86."""
    st.markdown('<div class="section-title">🍔 Carta (solo lectura)</div>',
                unsafe_allow_html=True)

    def _cards(rows):
        for r in rows:
            nombre = html.escape(_txt(r.get("nombre")))
            desc = html.escape(_txt(r.get("descripcion")))
            precio = int(r.get("precio", 0) or 0)
            desc_html = f'<div class="menu-precio">{desc}</div>' if desc else ""
            st.markdown(
                f'<div class="menu-card"><div class="menu-nombre">{nombre}</div>'
                f'{desc_html}'
                f'<div class="menu-precio">${fmt_money(precio)}</div></div>',
                unsafe_allow_html=True,
            )

    # 🍽️ Plato del Día (precio plano + componentes disponibles por grupo)
    st.markdown(
        f'<div style="background:#f9fafb; border:1px solid #e5e7eb; border-radius:10px; '
        f'padding:0.7rem 1rem; font-size:0.85rem; color:#374151; margin:0.5rem 0 1rem 0;">'
        f'🍽️ <b>Plato del Día</b> · ${fmt_money(precio_plato_dia())} · '
        f'elige {num_acompanamientos()} acompañamientos</div>',
        unsafe_allow_html=True,
    )
    por_grupo = componentes_activos_por_grupo()
    for grupo in GRUPOS_COMPONENTE:
        opciones = por_grupo.get(grupo, [])
        if not opciones:
            continue
        nombres = " · ".join(html.escape(str(o["nombre"])) for o in opciones)
        st.markdown(
            f'<div style="margin-bottom:0.6rem;"><span style="font-weight:600; color:#1a1a1a;">'
            f'{html.escape(GRUPO_LABEL.get(grupo, grupo))}:</span> '
            f'<span style="color:#6b7280; font-size:0.88rem;">{nombres}</span></div>',
            unsafe_allow_html=True,
        )

    cat = cargar_catalogo()
    disp = disponibles(cat) if not cat.empty else cat

    def _seccion(titulo: str, categoria: str):
        rows = (disp[disp["categoria"] == categoria].to_dict("records")
                if not disp.empty else [])
        st.markdown(f'<div class="section-title">{titulo}</div>', unsafe_allow_html=True)
        if rows:
            _cards(rows)
        else:
            st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">Sin platos activos.</p>',
                        unsafe_allow_html=True)

    _seccion("⭐ Especiales", "especial")
    _seccion("📋 A la carta", "a_la_carta")
    _seccion("🥤 Bebidas", "bebida")


def render():
    # RBAC: el mesero solo CONSULTA la carta. Bloquea todas las pestañas de edición,
    # los modales (crear/editar/eliminar) y los toggles (activo / 86).
    if not auth.can("edit_menu"):
        _render_readonly()
        return

    t1, t2, t3, t4, t5, t6 = st.tabs([
        "🍽️ Plato del Día", "⭐ Especiales", "📋 A la carta", "🥤 Bebidas",
        "📦 Inventario", "⚙️ Ajustes",
    ])
    with t1:
        _render_plato_dia()
    with t2:
        _render_catalogo_tab("especial", "Especiales", con_precio=False)
    with t3:
        _render_catalogo_tab("a_la_carta", "A la carta", con_precio=True)
    with t4:
        _render_catalogo_tab("bebida", "Bebidas", con_precio=True)
    with t5:
        _render_inventario()
    with t6:
        _render_ajustes()
