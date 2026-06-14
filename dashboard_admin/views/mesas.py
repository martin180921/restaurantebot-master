"""Vista de Mesas: gestión dinámica (alta, renombrar, activar y borrar) de mesas."""
import streamlit as st
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
import html

from db import engine


# ── Esquema defensivo ──────────────────────────────────────────────────────────
# El panel es un servicio aparte del bot; en un deploy nuevo puede cargar antes
# de que el bot ejecute init_db(). Garantizamos aquí lo que esta vista necesita.
def _ensure_schema():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS mesas (
                id      SERIAL PRIMARY KEY,
                nombre  VARCHAR(50)  NOT NULL,
                activa  BOOLEAN      NOT NULL DEFAULT TRUE,
                creada  TIMESTAMP    NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mesa_id INTEGER REFERENCES mesas(id)"
        ))


# ── DB: mesas ──────────────────────────────────────────────────────────────────
# P1: cacheamos el listado (cambia poco) y lo invalidamos tras cada escritura.
# Devolvemos dicts (no RowMapping) para que sean serializables por st.cache_data.
@st.cache_data(ttl=30)
def cargar_mesas():
    """Mesas + nº de pedidos activos asociados (para mostrar y para el borrado)."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT m.id, m.nombre, m.activa,
                   COALESCE(p.cnt, 0) AS pedidos_activos
            FROM mesas m
            LEFT JOIN (
                SELECT mesa_id, COUNT(*) AS cnt
                FROM pedidos
                WHERE mesa_id IS NOT NULL
                  AND estado NOT IN ('entregado', 'cancelado')
                GROUP BY mesa_id
            ) p ON p.mesa_id = m.id
            ORDER BY m.id
        """)).mappings().all()
    return [dict(r) for r in rows]

def crear_mesa(nombre: str):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO mesas (nombre) VALUES (:n)"), {"n": nombre.strip()})
    cargar_mesas.clear()  # P1

def renombrar_mesa(mesa_id: int, nombre: str):
    with engine.begin() as conn:
        conn.execute(text("UPDATE mesas SET nombre = :n WHERE id = :id"),
                     {"n": nombre.strip(), "id": mesa_id})
    cargar_mesas.clear()  # P1

def toggle_mesa(mesa_id: int, activa_actual: bool):
    with engine.begin() as conn:
        conn.execute(text("UPDATE mesas SET activa = :a WHERE id = :id"),
                     {"a": not activa_actual, "id": mesa_id})
    cargar_mesas.clear()  # P1

def eliminar_mesa(mesa_id: int) -> str:
    """Borra la mesa; si tiene pedidos asociados (historial) la archiva en su lugar.

    Devuelve 'deleted' o 'archived'. El borrado real falla con IntegrityError si
    algún pedido referencia la mesa (FK pedidos.mesa_id), así que en ese caso la
    desactivamos para conservar el historial.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM mesas WHERE id = :id"), {"id": mesa_id})
        resultado = "deleted"
    except IntegrityError:
        with engine.begin() as conn:
            conn.execute(text("UPDATE mesas SET activa = FALSE WHERE id = :id"), {"id": mesa_id})
        resultado = "archived"
    cargar_mesas.clear()  # P1
    return resultado


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: MESAS
# ══════════════════════════════════════════════════════════════════════════════
def render():
    _ensure_schema()
    mesas = cargar_mesas()

    col_lista, col_form = st.columns([2, 1])

    with col_lista:
        st.markdown('<div class="section-title">Mesas del restaurante</div>', unsafe_allow_html=True)

        activas   = sum(1 for m in mesas if m["activa"])
        inactivas = sum(1 for m in mesas if not m["activa"])
        ca, ci = st.columns(2)
        with ca:
            st.markdown(f'<div class="metric-card"><div class="metric-value metric-green">{activas}</div><div class="metric-label">Activas</div></div>', unsafe_allow_html=True)
        with ci:
            st.markdown(f'<div class="metric-card"><div class="metric-value" style="color:#9ca3af">{inactivas}</div><div class="metric-label">Inactivas</div></div>', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        if not mesas:
            st.markdown('<p style="color:#9ca3af; font-size:0.85rem;">Aún no hay mesas. Crea la primera en el panel de la derecha.</p>', unsafe_allow_html=True)
        else:
            for idx, row in enumerate(mesas):
                mid             = int(row["id"])
                nombre          = row["nombre"]
                activa          = bool(row["activa"])
                pedidos_activos = int(row["pedidos_activos"])
                card_cls = "menu-card" if activa else "menu-card inactivo"
                badge    = ('<span class="badge badge-activo">● Activa</span>' if activa
                           else '<span class="badge badge-inactivo">○ Inactiva</span>')
                sub = f"{pedidos_activos} pedido(s) activo(s)" if pedidos_activos else "Sin pedidos activos"

                col_card, col_btns = st.columns([3, 1])
                with col_card:
                    st.markdown(f"""
                    <div class="{card_cls}">
                      <div style="display:flex; justify-content:space-between; align-items:center;">
                        <div>
                          <div class="menu-nombre">🪑 {html.escape(str(nombre))}</div>
                          <div class="menu-precio">{sub}</div>
                        </div>
                        {badge}
                      </div>
                    </div>
                    """, unsafe_allow_html=True)

                with col_btns:
                    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
                    b1, b2, b3 = st.columns(3)
                    with b1:
                        toggle_label = "⏸" if activa else "▶"
                        toggle_help  = "Desactivar" if activa else "Activar"
                        if st.button(toggle_label, key=f"mesa_toggle_{mid}_{idx}", help=toggle_help):
                            toggle_mesa(mid, activa)
                            st.rerun()
                    with b2:
                        if st.button("✏️", key=f"mesa_edit_{mid}_{idx}", help="Renombrar"):
                            st.session_state["editar_mesa_id"]     = mid
                            st.session_state["editar_mesa_nombre"] = nombre
                            st.rerun()
                    with b3:
                        if st.button("🗑", key=f"mesa_del_{mid}_{idx}", help="Eliminar"):
                            st.session_state["confirmar_del_mesa"] = mid
                            st.rerun()

                if st.session_state.get("confirmar_del_mesa") == mid:
                    st.warning(f"¿Eliminar **{nombre}**? Si tiene pedidos en su historial, se archivará en lugar de borrarse.")
                    cc1, cc2 = st.columns(2)
                    with cc1:
                        if st.button("Sí, eliminar", key=f"mesa_si_del_{mid}", type="primary"):
                            resultado = eliminar_mesa(mid)
                            st.session_state.pop("confirmar_del_mesa", None)
                            if resultado == "archived":
                                st.info(f"**{nombre}** tenía pedidos asociados, así que se archivó (inactiva) para conservar el historial.")
                            st.rerun()
                    with cc2:
                        if st.button("Cancelar", key=f"mesa_no_del_{mid}"):
                            st.session_state.pop("confirmar_del_mesa", None)
                            st.rerun()

    with col_form:
        editando = "editar_mesa_id" in st.session_state
        titulo   = "Renombrar mesa" if editando else "Nueva mesa"
        st.markdown(f'<div class="section-title">{titulo}</div>', unsafe_allow_html=True)

        nombre_default = st.session_state.get("editar_mesa_nombre", "")
        nombre_input = st.text_input("Nombre de la mesa", value=nombre_default,
                                     placeholder="Ej: Mesa 1, Terraza, Barra…", key="input_mesa_nombre")

        b_guardar, b_cancelar = st.columns(2)
        with b_guardar:
            if st.button("💾 Guardar", type="primary", key="btn_guardar_mesa"):
                if not nombre_input.strip():
                    st.error("El nombre no puede estar vacío.")
                else:
                    if editando:
                        renombrar_mesa(st.session_state["editar_mesa_id"], nombre_input)
                        st.session_state.pop("editar_mesa_id", None)
                        st.session_state.pop("editar_mesa_nombre", None)
                        st.success("Mesa actualizada ✓")
                    else:
                        crear_mesa(nombre_input)
                        st.success("Mesa creada ✓")
                    st.rerun()
        with b_cancelar:
            if editando:
                if st.button("✕ Cancelar", key="btn_cancelar_mesa"):
                    st.session_state.pop("editar_mesa_id", None)
                    st.session_state.pop("editar_mesa_nombre", None)
                    st.rerun()

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("""
        <div style="background:#f9fafb; border:1px solid #e5e7eb; border-radius:10px; padding:1rem; font-size:0.78rem; color:#6b7280;">
          <div style="color:#374151; font-weight:600; margin-bottom:6px;">💡 Cómo funciona</div>
          Crea tantas mesas como necesites.<br><br>
          <b style="color:#4b5563;">Desactivar</b> oculta la mesa para nuevos pedidos sin borrarla.<br>
          <b style="color:#4b5563;">Eliminar</b> la borra; si ya tiene pedidos, se archiva para conservar el historial.
        </div>
        """, unsafe_allow_html=True)
