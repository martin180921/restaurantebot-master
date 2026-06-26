"""Vista Personal: perfiles PERSISTENTES de empleados (mesero/caja/admin) + accesos de
turno efímeros (legado).

FASE 1 amplía esta vista de "solo PINs de turno del mesero" a una gestión de personal:
el admin/caja crea perfiles con nombre, rol y PIN propio (empleados.py). Cada empleado
entra con su PIN, queda identificado en la auditoría y su entrada/salida se marca solo
(clock-in/out). El PIN se muestra UNA vez al crearlo o regenerarlo.

Se conserva el flujo anterior de PIN EFÍMERO de turno (mesero_keys) en un panel aparte
para no romper despliegues en marcha; los dos sistemas de acceso conviven.
"""
import streamlit as st
import html

import auth
import audit
import empleados
import mesero_keys
from db import flash


_ROL_LABEL = {"mesero": "🧑‍🍳 Mesero", "caja": "💵 Caja", "admin": "🛡️ Admin"}


# ── Modal: alta de empleado ──────────────────────────────────────────────────────
@st.dialog("➕ Nuevo empleado")
def _dialog_nuevo_empleado():
    st.markdown("Crea un perfil de personal con su PIN de acceso. El PIN se muestra una "
                "sola vez tras crearlo.")
    nombre = st.text_input("Nombre", key="emp_nombre", placeholder="Ej: Carlos Pérez")
    rol = st.selectbox("Rol", ["mesero", "caja", "admin"], key="emp_rol",
                       format_func=lambda r: _ROL_LABEL[r])
    usar_custom = st.checkbox("Definir el PIN manualmente (si no, se genera uno)",
                              key="emp_custom")
    pin = ""
    if usar_custom:
        pin = st.text_input("PIN (4 a 6 dígitos)", key="emp_pin", max_chars=6,
                            placeholder="Ej: 4821")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Crear empleado", key="emp_crear", type="primary",
                     use_container_width=True, disabled=not nombre.strip()):
            pin_claro, err = empleados.crear_empleado(
                nombre, rol, pin, auth.actor()[0])
            if err:
                st.session_state["_emp_msg"] = err
            else:
                audit.registrar("empleado_creado", "empleado", None,
                                {"nombre": nombre.strip(), "rol": rol})
                st.session_state["_emp_pin"] = (nombre.strip(), rol, pin_claro)
            st.rerun()
    with c2:
        if st.button("Volver", key="emp_volver", use_container_width=True):
            st.rerun()


# ── Modal: borrado permanente de empleado ────────────────────────────────────────
@st.dialog("🗑 Eliminar empleado")
def _dialog_eliminar_empleado(eid: int, nombre: str, rol: str):
    st.markdown(
        f"¿Seguro que quieres **eliminar de forma permanente** a "
        f"**{html.escape(nombre)}** ({_ROL_LABEL.get(rol, rol)})?  \n"
        "Esta acción **no se puede deshacer**. Su historial en la auditoría se conserva."
    )
    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗑 Sí, eliminar", key=f"confirm_del_{eid}", type="primary",
                     use_container_width=True):
            if empleados.eliminar_empleado(eid):
                audit.registrar("empleado_eliminado", "empleado", eid, {"nombre": nombre})
                flash(f"Empleado eliminado · {nombre}", "🗑")
            st.rerun()
    with c2:
        if st.button("Volver", key=f"cancel_del_{eid}", use_container_width=True):
            st.rerun()


def _mostrar_pin_generado():
    """Muestra (una vez) el PIN recién creado/regenerado y un aviso de error si lo hubo."""
    err = st.session_state.pop("_emp_msg", None)
    if err:
        st.error(err)
    info = st.session_state.get("_emp_pin")
    if info:
        nombre, rol, pin = info
        st.success(f"Empleado **{html.escape(nombre)}** ({_ROL_LABEL.get(rol, rol)}) listo. "
                   "Entrégale su PIN (no se vuelve a mostrar):")
        st.code(pin, language=None)
        if st.button("Entendido, ocultar", key="emp_ocultar", use_container_width=True):
            st.session_state.pop("_emp_pin", None)
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: PERSONAL
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Gestión de personal: admin/caja (defensa en profundidad sobre el router).
    if not auth.can("manage_caja"):
        st.error("🔒 Acceso denegado")
        st.stop()

    st.markdown('<div class="section-title">👤 Personal · empleados y accesos</div>',
                unsafe_allow_html=True)
    st.markdown(
        '<p style="color:#6b7280; font-size:0.85rem; margin-top:-6px;">Cada empleado entra '
        'con su PIN personal y queda identificado en la auditoría; su entrada y salida se '
        'registran solas. El PIN se muestra una sola vez al crearlo.</p>',
        unsafe_allow_html=True,
    )

    # Solo el ADMINISTRADOR crea/gestiona perfiles de empleado. Caja conserva los accesos
    # de turno efímeros (abajo) para dar acceso temporal a un mesero sin perfil fijo.
    puede_gestionar = auth.can("manage_empleados")

    _mostrar_pin_generado()

    if puede_gestionar:
        if st.button("➕ Nuevo empleado", key="btn_nuevo_emp", use_container_width=True):
            _dialog_nuevo_empleado()
    else:
        st.caption("🔒 Solo un administrador puede crear o gestionar perfiles de empleado. "
                   "Como caja puedes generar accesos de turno efímeros (más abajo).")

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-title">Empleados</div>', unsafe_allow_html=True)

    emps = empleados.listar_empleados(incluir_inactivos=True)
    # Empleados con turno abierto ahora (marca "🟢 en turno" en su tarjeta).
    en_turno = {int(s["empleado_id"]) for s in empleados.sesiones_activas()
                if s.get("empleado_id")}

    if not emps:
        st.caption("Aún no hay empleados. Crea el primero con ➕ Nuevo empleado.")
    for e in emps:
        eid = int(e["id"])
        nombre = str(e["nombre"])
        rol = str(e["rol"])
        activo = bool(e["activo"])
        bloqueado = bool(e.get("bloqueado"))
        # Estado en vivo: inactivo / acceso cerrado (bloqueado) / En turno / Fuera de turno.
        if not activo:
            estado_chip = '<span style="color:#9ca3af;"> · perfil inactivo</span>'
            borde = "#9ca3af"
        elif bloqueado:
            estado_chip = '<span style="color:#b45309;"> · 🔒 Acceso cerrado</span>'
            borde = "#d97706"
        elif eid in en_turno:
            estado_chip = '<span style="color:#16a34a;"> · 🟢 En turno</span>'
            borde = "#16a34a"
        else:
            estado_chip = '<span style="color:#9ca3af;"> · ⚪ Fuera de turno</span>'
            borde = "#d1d5db"
        # Tarjeta + 3 acciones: Salida/Reactivar (caja+admin) · Nuevo PIN · Baja (admin).
        col_a, col_acc, col_pin, col_baja = st.columns([3, 1.3, 1, 1])
        with col_a:
            st.markdown(
                f'<div class="order-card" style="border-left:4px solid {borde};">'
                f'<div style="font-size:0.9rem; color:#1a1a1a; font-weight:600;">'
                f'{_ROL_LABEL.get(rol, rol)} · {html.escape(nombre)}'
                f'<span style="font-weight:400; font-size:0.8rem;">{estado_chip}</span>'
                f'</div></div>',
                unsafe_allow_html=True,
            )
        # Salida (cerrar turno) / Reactivar acceso: lo hace CAJA o admin (manage_caja). La
        # vista ya está restringida a ese candado, así que aquí siempre está disponible.
        with col_acc:
            if activo and not bloqueado:
                if st.button("⏹ Salida", key=f"salida_{eid}", use_container_width=True,
                             help="Cerrar turno: bloquea el PIN y cierra su sesión ahora"):
                    if empleados.bloquear_acceso(eid):
                        audit.registrar("acceso_bloqueado", "empleado", eid, {"nombre": nombre})
                        flash(f"Turno cerrado · {nombre} (su acceso queda bloqueado)", "🔒")
                    st.rerun()
            elif activo and bloqueado:
                if st.button("▶ Reactivar", key=f"reactivar_{eid}", use_container_width=True,
                             help="Reabrir el acceso: su PIN vuelve a servir"):
                    if empleados.reactivar_acceso(eid):
                        audit.registrar("acceso_reactivado", "empleado", eid, {"nombre": nombre})
                        flash(f"Acceso reactivado · {nombre}", "▶")
                    st.rerun()
        # Acciones de PERFIL: solo admin (manage_empleados). Caja las ve deshabilitadas/ocultas.
        with col_pin:
            if puede_gestionar and activo and st.button("🔑 Nuevo PIN", key=f"regen_{eid}",
                                                        use_container_width=True):
                pin, err = empleados.regenerar_pin(eid)
                if err:
                    st.session_state["_emp_msg"] = err
                else:
                    audit.registrar("pin_regenerado", "empleado", eid, {"nombre": nombre})
                    st.session_state["_emp_pin"] = (nombre, rol, pin)
                st.rerun()
        with col_baja:
            if puede_gestionar and activo:
                if st.button("⏹ Baja", key=f"baja_{eid}", use_container_width=True,
                             help="Dar de baja el perfil (desactiva su acceso; reversible borrándolo o recreándolo)"):
                    if empleados.desactivar_empleado(eid):
                        audit.registrar("empleado_baja", "empleado", eid, {"nombre": nombre})
                        flash(f"Empleado dado de baja · {nombre}", "⏹")
                    st.rerun()
            elif puede_gestionar and not activo:
                # Perfil ya inactivo: ofrecemos el borrado PERMANENTE (con confirmación).
                if st.button("🗑 Eliminar", key=f"del_{eid}", use_container_width=True,
                             help="Borrar el perfil de forma permanente"):
                    _dialog_eliminar_empleado(eid, nombre, rol)

    # ── Accesos de turno efímeros (legado, opcional) ────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander("🔑 Accesos de turno efímeros (legado)"):
        st.caption("PIN temporal para un mesero sin perfil fijo; se revoca al cerrar la caja. "
                   "Para personal habitual usa un empleado con PIN propio (arriba).")

        pin = st.session_state.get("_pin_generado")
        if pin is not None:
            if pin == "ERROR":
                st.error("No se pudo generar el PIN. Intenta de nuevo.")
            else:
                st.success("PIN de turno generado · entrégaselo al mesero:")
                st.code(pin, language=None)
            if st.button("Ocultar", key="ocultar_pin", use_container_width=True):
                st.session_state.pop("_pin_generado", None)
                st.rerun()

        etiqueta = st.text_input("Nombre del mesero (opcional)", key="nuevo_mesero_nombre",
                                 placeholder="Ej: Temporal sábado")
        if st.button("Generar PIN de turno", key="btn_gen_efimero", use_container_width=True):
            st.session_state["_pin_generado"] = (
                mesero_keys.generar_clave(etiqueta, auth.actor()[0]) or "ERROR")
            st.rerun()

        activas_ef = mesero_keys.claves_activas()
        if activas_ef:
            st.markdown("**Accesos de turno activos**")
            for k in activas_ef:
                kid = int(k["id"])
                nombre = str(k.get("etiqueta") or "Mesero")
                col_a, col_b = st.columns([3, 1])
                with col_a:
                    st.markdown(f'🔑 {html.escape(nombre)}')
                with col_b:
                    if st.button("⏹ Cerrar", key=f"revoke_mesero_{kid}",
                                 use_container_width=True):
                        mesero_keys.revocar_clave(kid)
                        flash(f"Acceso cerrado · {nombre}", "🔒")
                        st.rerun()
