"""Vista Personal (solo administrador): informe de actividad por empleado + libro mayor.

Vive como pestaña dentro de Caja (junto a Resumen y Cancelaciones) y comparte su candado
'see_revenue': muestra montos cobrados por persona, así que solo el admin la instancia.
Se nutre del libro mayor (auditoria) y del marcaje de turno (sesiones_empleado) vía
audit.reporte_personal() — la fuente única de verdad de "quién hizo qué".

Tres bloques:
  1. Quién está EN TURNO ahora (clock-in en vivo).
  2. Tabla por empleado en un rango de fechas: horas, cobros (nº y monto), cancelaciones,
     descuentos (nº y monto). El detector de fuga: cruzar cancelaciones/descuentos por
     persona contra sus ventas.
  3. Libro mayor reciente (eventos crudos) con filtro por acción.
"""
import streamlit as st
import pandas as pd
from datetime import timedelta

import auth
import audit
import empleados
import remember
from db import fmt_money, titulo_seccion, hoy_bogota


# Etiquetas legibles de las acciones del libro mayor.
_ACCION_LABEL = {
    "cobrar":            "💵 Cobro",
    "cancelar_pedido":   "✕ Cancelación",
    "descuento":         "🏷️ Descuento",
    "cortesia":          "🎁 Cortesía",
    "checkout_iniciado": "🔓 Checkout iniciado",
    "clock_in":          "🟢 Entrada",
    "clock_out":         "🔴 Salida",
    "empleado_creado":   "➕ Alta empleado",
    "empleado_baja":     "➖ Baja empleado",
    "gasto_caja":        "🧾 Gasto de caja",
    "reingreso_gasto":   "↩️ Devolución gasto",
    "base_repartidor":   "🛵 Base repartidor",
    "retorno_base":      "🟢 Float devuelto",
    "caja_apertura":     "🟢 Apertura de caja",
    "caja_cierre":       "🔒 Cierre de caja",
}


def render():
    # Defensa en profundidad: el router solo crea esta pestaña para quien ve ingresos.
    if not auth.can("see_revenue"):
        st.error("🔒 Acceso denegado")
        st.stop()

    st.markdown(titulo_seccion('👥 Personal · actividad y auditoría'),
                unsafe_allow_html=True)

    # Finaliza sesiones sin latido (pestañas cerradas sin "Salir") antes de calcular horas,
    # fijando su salida en el último momento visto → horas exactas y "en turno" fiel.
    empleados.cerrar_sesiones_inactivas()
    # Housekeeping del "recuérdame" de admin/caja: borra tokens ya vencidos (no afecta
    # a los vigentes; validar() ya los ignora aunque no se hayan borrado todavía).
    remember.limpiar_expiradas()

    # ── 1. En turno ahora (clock-in en vivo) ────────────────────────────────────
    activos = empleados.sesiones_activas()
    if activos:
        chips = "".join(
            f'<span style="display:inline-block; background:#dcfce7; color:#15803d; '
            f'border:1px solid #bbf7d0; border-radius:999px; padding:4px 12px; '
            f'margin:0 6px 6px 0; font-size:0.78rem; font-weight:600;">🟢 {s["nombre"]} '
            f'<span style="color:#6b6b64; font-weight:400;">· {s["rol"]} · desde '
            f'{s["login_at"].strftime("%H:%M") if hasattr(s["login_at"], "strftime") else "—"}'
            f'</span></span>'
            for s in activos
        )
        st.markdown(f'<div style="margin-bottom:10px;">{chips}</div>', unsafe_allow_html=True)
    else:
        st.caption("Nadie con turno abierto en este momento.")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── 2. Informe por empleado en un rango ─────────────────────────────────────
    c1, c2 = st.columns(2)
    with c1:
        desde = st.date_input("Desde", value=hoy_bogota() - timedelta(days=7),
                              format="DD/MM/YYYY", key="rp_desde")
    with c2:
        hasta = st.date_input("Hasta", value=hoy_bogota(),
                              format="DD/MM/YYYY", key="rp_hasta")
    if desde > hasta:
        st.warning("El rango es inválido: 'Desde' es posterior a 'Hasta'.")
        return

    filas = audit.reporte_personal(desde, hasta)
    if not filas:
        st.markdown('<p style="color:#a3a39b; font-size:0.9rem; padding:1rem 0;">'
                    'Sin actividad registrada en este rango.</p>', unsafe_allow_html=True)
    else:
        tabla = pd.DataFrame([{
            "Empleado":      f["actor"],
            "Rol":           f["rol"],
            "Horas":         f["horas"],
            "Cobros":        f["cobros_n"],
            "$ Cobrado":     f"${fmt_money(f['cobros_monto'])}",
            "Cancelac.":     f["cancel_n"],
            "Descuentos":    f["desc_n"],
            "$ Descontado":  f"${fmt_money(f['desc_monto'])}",
        } for f in filas])
        st.dataframe(tabla, hide_index=True, use_container_width=True)

        # Señal de fuga: quien acumula cancelaciones/descuentos desproporcionados.
        sospechosos = [f for f in filas if (f["cancel_n"] + f["desc_n"]) >= 3]
        if sospechosos:
            nombres = ", ".join(f'{f["actor"]} ({f["cancel_n"]} canc · {f["desc_n"]} desc)'
                                for f in sospechosos)
            st.caption(f"⚠️ Revisar concentración de anulaciones/descuentos: {nombres}.")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── 3. Libro mayor reciente (eventos crudos) ────────────────────────────────
    st.markdown(titulo_seccion('📒 Libro mayor · eventos recientes'),
                unsafe_allow_html=True)
    opciones = {"Todas": None, "💵 Cobros": "cobrar", "✕ Cancelaciones": "cancelar_pedido",
                "🏷️ Descuentos": "descuento", "🎁 Cortesías": "cortesia",
                "🟢 Entradas": "clock_in", "🔴 Salidas": "clock_out"}
    sel = st.selectbox("Filtrar por acción", list(opciones.keys()), key="rp_accion")
    eventos = audit.cargar_auditoria(limite=150, accion=opciones[sel])
    if not eventos:
        st.caption("Sin eventos para este filtro.")
        return
    for e in eventos:
        ts = e["ts"].strftime("%d/%m %H:%M") if hasattr(e["ts"], "strftime") else "—"
        etiqueta = _ACCION_LABEL.get(e["accion"], e["accion"])
        actor_txt = e.get("actor_nombre") or "—"
        det = e.get("detalle") or {}
        extra = ""
        if isinstance(det, dict):
            if det.get("monto"):
                extra = f' · ${fmt_money(det["monto"])}'
            if det.get("motivo"):
                extra += f' · {det["motivo"]}'
            if det.get("titulo"):
                extra += f' · {det["titulo"]}'
        ref = f' · #{e["entidad_id"]}' if e.get("entidad_id") else ""
        st.markdown(
            f'<div style="display:flex; justify-content:space-between; font-size:0.8rem; '
            f'padding:6px 0; border-bottom:1px solid #f2f1ed;">'
            f'<span style="color:#45443e;">{etiqueta}{ref} '
            f'<span style="color:#a3a39b;">{extra}</span></span>'
            f'<span style="color:#6b6b64;">{actor_txt} · {ts}</span></div>',
            unsafe_allow_html=True,
        )
