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

from db import (engine, cargar_menu, cargar_componentes, cargar_catalogo,
                cargar_ajustes, fmt_money, flash,
                GRUPOS_COMPONENTE, GRUPO_LABEL, precio_especiales)


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
    agotado = _agotado_hoy(row.get("agotado_hasta"))
    card_cls = "menu-card" if (activo and not agotado) else "menu-card inactivo"
    if agotado:
        estado = '<span class="badge badge-agotado">🚫 Hoy</span>'
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
        if agotado:
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
    agotado = _agotado_hoy(row.get("agotado_hasta"))
    desc = _txt(row.get("descripcion"))
    card_cls = "menu-card" if (activo and not agotado) else "menu-card inactivo"
    badge = ('<span class="badge badge-activo">● Activo</span>' if activo
             else '<span class="badge badge-inactivo">○ Inactivo</span>')
    if agotado:
        badge += ' <span class="badge badge-agotado">🚫 Hoy</span>'

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
            if agotado:
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
# SECCIÓN: MENÚ
# ══════════════════════════════════════════════════════════════════════════════
def render():
    t1, t2, t3, t4, t5 = st.tabs([
        "🍽️ Plato del Día", "⭐ Especiales", "📋 A la carta", "🥤 Bebidas", "⚙️ Ajustes",
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
        _render_ajustes()
