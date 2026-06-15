"""Vista de Menú: alta, edición, activación, agotado-hoy y borrado de platos."""
import streamlit as st
from sqlalchemy import text
import html
import pandas as pd
from datetime import date

from db import engine, cargar_menu, fmt_money, flash


# ── DB: menú ───────────────────────────────────────────────────────────────────
def agregar_plato(nombre: str, precio: int):
    with engine.begin() as conn:
        max_orden = conn.execute(text("SELECT COALESCE(MAX(orden),0) FROM menu")).scalar()
        conn.execute(text(
            "INSERT INTO menu (nombre, precio, activo, orden) VALUES (:n, :p, TRUE, :o)"
        ), {"n": nombre.strip(), "p": precio, "o": max_orden + 1})
    cargar_menu.clear()  # P1: invalida la caché del menú
    flash("Plato agregado", "✅")

def actualizar_plato(plato_id: int, nombre: str, precio: int):
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE menu SET nombre = :n, precio = :p WHERE id = :id"
        ), {"n": nombre.strip(), "p": precio, "id": plato_id})
    cargar_menu.clear()  # P1
    flash("Plato actualizado", "✅")

def toggle_plato(plato_id: int, activo_actual: bool):
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE menu SET activo = :a WHERE id = :id"
        ), {"a": not activo_actual, "id": plato_id})
    cargar_menu.clear()  # P1
    flash("Plato activado" if not activo_actual else "Plato desactivado",
          "▶️" if not activo_actual else "⏸️")

def eliminar_plato(plato_id: int):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM menu WHERE id = :id"), {"id": plato_id})
    cargar_menu.clear()  # P1
    flash("Plato eliminado", "🗑️")

# F6: "86 / agotado hoy" — se marca con la fecha de hoy y vuelve a estar
# disponible solo (automáticamente) al cambiar el día.
def marcar_agotado(plato_id: int):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu SET agotado_hasta = CURRENT_DATE WHERE id = :id"),
                     {"id": plato_id})
    cargar_menu.clear()
    flash("Marcado agotado por hoy", "🚫")

def quitar_agotado(plato_id: int):
    with engine.begin() as conn:
        conn.execute(text("UPDATE menu SET agotado_hasta = NULL WHERE id = :id"),
                     {"id": plato_id})
    cargar_menu.clear()
    flash("Disponible de nuevo", "♻️")


# ── Modal: eliminar plato (Fase 3) ──────────────────────────────────────────────
@st.dialog("Eliminar plato")
def _dialog_eliminar_plato(pid: int, nombre: str):
    st.markdown(
        f"¿Eliminar **{html.escape(str(nombre))}** permanentemente?  \n"
        "Esta acción no se puede deshacer."
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑 Sí, eliminar", key=f"confirm_eliminar_{pid}", type="primary",
                     use_container_width=True):
            eliminar_plato(pid)   # flashea toast
            st.rerun()
    with c2:
        if st.button("Volver", key=f"volver_eliminar_{pid}", use_container_width=True):
            st.rerun()

def _agotado_hoy(valor) -> bool:
    """True si agotado_hasta cubre el día de hoy."""
    if valor is None or pd.isna(valor):
        return False
    try:
        return pd.Timestamp(valor).date() >= date.today()
    except (ValueError, TypeError):
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: MENÚ
# ══════════════════════════════════════════════════════════════════════════════
def render():
    df_menu = cargar_menu()

    # Fix 4: tighter column proportions [2, 1] to bring buttons closer
    col_lista, col_form = st.columns([2, 1])

    with col_lista:
        st.markdown('<div class="section-title">Platos actuales</div>', unsafe_allow_html=True)

        activos   = len(df_menu[df_menu["activo"] == True])
        inactivos = len(df_menu[df_menu["activo"] == False])
        ca, ci = st.columns(2)
        with ca:
            st.markdown(f'<div class="metric-card"><div class="metric-value metric-green">{activos}</div><div class="metric-label">Activos</div></div>', unsafe_allow_html=True)
        with ci:
            st.markdown(f'<div class="metric-card"><div class="metric-value" style="color:#9ca3af">{inactivos}</div><div class="metric-label">Inactivos</div></div>', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        if df_menu.empty:
            st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">No hay platos en el menú.</p>', unsafe_allow_html=True)
        else:
            for idx, row in df_menu.iterrows():
                pid        = int(row["id"])
                nombre     = row["nombre"]
                precio     = int(row["precio"])
                activo     = bool(row["activo"])
                agotado    = _agotado_hoy(row.get("agotado_hasta"))  # F6
                card_cls   = "menu-card" if (activo and not agotado) else "menu-card inactivo"
                badge      = '<span class="badge badge-activo">● Activo</span>' if activo else '<span class="badge badge-inactivo">○ Inactivo</span>'
                if agotado:
                    badge += ' <span class="badge badge-agotado">🚫 Hoy</span>'

                # Fix 4 / F6: 4 acciones, columna un poco más ancha
                col_card, col_btns = st.columns([3, 1.4])
                with col_card:
                    st.markdown(f"""
                    <div class="{card_cls}">
                      <div style="display:flex; justify-content:space-between; align-items:center;">
                        <div>
                          <div class="menu-nombre">{html.escape(str(nombre))}</div>
                          <div class="menu-precio">${fmt_money(precio)}</div>
                        </div>
                        <div style="text-align:right;">{badge}</div>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)

                with col_btns:
                    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
                    b1, b2, b3, b4 = st.columns(4)
                    with b1:
                        toggle_label = "⏸" if activo else "▶"
                        toggle_help  = "Desactivar" if activo else "Activar"
                        if st.button(toggle_label, key=f"toggle_{pid}_{idx}", help=toggle_help,
                                     use_container_width=True):
                            toggle_plato(pid, activo)
                            st.rerun()
                    with b2:
                        # F6: 86 = agotar hoy; ♻ = volver a habilitar
                        if agotado:
                            if st.button("♻", key=f"unago_{pid}_{idx}", help="Disponible de nuevo",
                                         use_container_width=True):
                                quitar_agotado(pid)
                                st.rerun()
                        else:
                            if st.button("86", key=f"ago_{pid}_{idx}", help="Agotado hoy (vuelve mañana)",
                                         use_container_width=True):
                                marcar_agotado(pid)
                                st.rerun()
                    with b3:
                        if st.button("✏️", key=f"edit_{pid}_{idx}", help="Editar",
                                     use_container_width=True):
                            st.session_state["editar_id"]     = pid
                            st.session_state["editar_nombre"] = nombre
                            st.session_state["editar_precio"] = precio
                            st.rerun()
                    with b4:
                        # Fase 3: modal centrado en vez de aviso inline.
                        if st.button("🗑", key=f"eliminar_{pid}_{idx}", help="Eliminar",
                                     use_container_width=True):
                            _dialog_eliminar_plato(pid, nombre)

    with col_form:
        editando = "editar_id" in st.session_state
        titulo   = "Editar plato" if editando else "Agregar plato"
        st.markdown(f'<div class="section-title">{titulo}</div>', unsafe_allow_html=True)

        nombre_default = st.session_state.get("editar_nombre", "")
        precio_default = st.session_state.get("editar_precio", 0)

        nombre_input = st.text_input("Nombre del plato", value=nombre_default, key="input_nombre")
        precio_input = st.number_input("Precio", min_value=0, value=precio_default, step=1000, key="input_precio")

        b_guardar, b_cancelar = st.columns(2)
        with b_guardar:
            if st.button("💾 Guardar", type="primary", key="btn_guardar",
                         use_container_width=True):
                if not nombre_input.strip():
                    st.error("El nombre no puede estar vacío.")
                elif precio_input <= 0:
                    st.error("El precio debe ser mayor a 0.")
                else:
                    if editando:
                        actualizar_plato(st.session_state["editar_id"], nombre_input, precio_input)
                        st.session_state.pop("editar_id", None)
                        st.session_state.pop("editar_nombre", None)
                        st.session_state.pop("editar_precio", None)
                    else:
                        agregar_plato(nombre_input, precio_input)
                    st.rerun()  # el toast lo emite db.flash tras el rerun

        with b_cancelar:
            if editando:
                if st.button("✕ Cancelar", key="btn_cancelar", use_container_width=True):
                    st.session_state.pop("editar_id", None)
                    st.session_state.pop("editar_nombre", None)
                    st.session_state.pop("editar_precio", None)
                    st.rerun()

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("""
        <div style="background:#f9fafb; border:1px solid #e5e7eb; border-radius:10px; padding:1rem; font-size:0.78rem; color:#6b7280;">
          <div style="color:#374151; font-weight:600; margin-bottom:6px;">💡 Cómo funciona</div>
          Los cambios se reflejan en la carta de inmediato.<br><br>
          <b style="color:#4b5563;">86</b> marca el plato como agotado solo por hoy; vuelve solo mañana.<br>
          <b style="color:#4b5563;">Desactivar</b> (⏸) oculta el plato del menú sin borrarlo.<br>
          <b style="color:#4b5563;">Eliminar</b> (🗑) lo borra permanentemente.
        </div>
        """, unsafe_allow_html=True)
