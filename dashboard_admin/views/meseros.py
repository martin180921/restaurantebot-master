"""Vista Meseros: accesos de turno (PINs efímeros) del personal de mesa.

El mesero no tiene contraseña fija: aquí el cajero/admin le genera un PIN de turno que
solo funciona mientras esté activo, y lo revoca al terminar el turno (o se revocan todos
al cerrar la caja). El PIN se muestra UNA sola vez al generarlo. Ver mesero_keys.py.
"""
import streamlit as st
import html

import auth
import mesero_keys
from db import flash


# ── Modal: generar un nuevo acceso ──────────────────────────────────────────────
@st.dialog("🔑 Nuevo acceso de mesero")
def _dialog_generar_mesero():
    st.markdown("Genera un PIN de turno para que el mesero acceda. El PIN se muestra una "
                "sola vez tras generarlo; entrégaselo al mesero.")
    nombre = st.text_input("Nombre del mesero (opcional)", key="nuevo_mesero_nombre",
                           placeholder="Ej: Carlos")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔑 Generar PIN", key="gen_pin", type="primary", use_container_width=True):
            # Guardamos el PIN en sesión y cerramos el modal: se muestra prominente en la
            # vista (un st.rerun dentro del diálogo lo cierra; mostrarlo fuera es más claro).
            st.session_state["_pin_generado"] = (
                mesero_keys.generar_clave(nombre, auth.current_role() or "") or "ERROR")
            st.rerun()
    with c2:
        if st.button("Volver", key="gen_volver", use_container_width=True):
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: MESEROS · ACCESOS DE TURNO
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Solo el personal de caja/admin gestiona los accesos del mesero (defensa en
    # profundidad: el router ya restringe la vista, revalidamos por si acaso).
    if not auth.can("manage_caja"):
        st.error("🔒 Acceso denegado")
        st.stop()

    st.markdown('<div class="section-title">👤 Meseros · accesos de turno</div>',
                unsafe_allow_html=True)
    st.markdown(
        '<p style="color:#6b7280; font-size:0.85rem; margin-top:-6px;">El mesero entra con un '
        'PIN de turno (no hay contraseña fija). Genera uno al iniciar su turno y ciérralo al '
        'terminar; al cerrar la caja se revocan todos.</p>',
        unsafe_allow_html=True,
    )

    # PIN recién generado: mostrarlo UNA vez, prominente, hasta que el cajero lo oculte.
    pin = st.session_state.get("_pin_generado")
    if pin is not None:
        if pin == "ERROR":
            st.error("No se pudo generar el PIN. Intenta de nuevo.")
        else:
            st.success("PIN de turno generado · entrégaselo al mesero (no se vuelve a mostrar):")
            st.code(pin, language=None)
        if st.button("Entendido, ocultar", key="ocultar_pin", use_container_width=True):
            st.session_state.pop("_pin_generado", None)
            st.session_state.pop("nuevo_mesero_nombre", None)
            st.rerun()

    if st.button("➕ Generar acceso de mesero", key="btn_gen_mesero", use_container_width=True):
        _dialog_generar_mesero()

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-title">Accesos activos</div>', unsafe_allow_html=True)

    activas = mesero_keys.claves_activas()
    if not activas:
        st.caption("Sin accesos activos. Genera un PIN cuando un mesero entre a su turno.")
        return
    for k in activas:
        kid = int(k["id"])
        nombre = str(k.get("etiqueta") or "Mesero")
        try:
            cuando = k["creada"].strftime("%d/%m %H:%M")
        except Exception:
            cuando = "—"
        col_a, col_b = st.columns([3, 1])
        with col_a:
            st.markdown(
                f'<div class="order-card" style="border-left:4px solid #16a34a;">'
                f'<div style="font-size:0.85rem; color:#374151;">🔑 <b>{html.escape(nombre)}</b> · '
                f'activo desde {cuando}</div></div>',
                unsafe_allow_html=True,
            )
        with col_b:
            if st.button("⏹ Cerrar turno", key=f"revoke_mesero_{kid}", use_container_width=True):
                mesero_keys.revocar_clave(kid)
                flash(f"Acceso cerrado · {nombre}", "🔒")
                st.rerun()
