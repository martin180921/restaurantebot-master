"""Vista de Facturas (solo administrador): documentos fiscales emitidos (bloque C del
plan de facturación electrónica, ver docs/plan_facturacion_y_datafono.md).

Vive como pestaña dentro de Administración, junto a Resumen/Cancelaciones/Personal/
Actividad, y comparte su candado: solo el rol con capacidad 'see_revenue' (admin) la
instancia. Lista los documentos del libro 'documentos_fiscales' (más nuevo primero), con
reimpresión y anulación (nota crédito) de los ya emitidos. Reenvío por email queda fuera
de esta tanda: llega junto al proveedor real (PAC), que es quien de verdad entrega el
PDF/XML al cliente — ver docs/plan_facturacion_y_datafono.md.

Si la facturación electrónica está apagada en Ajustes, esta pestaña sigue existiendo
(el admin puede seguir revisando documentos ya emitidos); simplemente no se generan
nuevos mientras el flag siga en OFF.
"""
import streamlit as st
import json
import html

import auth
import audit
import facturacion
from db import fmt_money, titulo_seccion
from views import pedidos

_ESTADO_COLOR = {
    "emitido":   "#16a34a",
    "rechazado": "#dc2626",
    "anulado":   "#6b6b64",
    "borrador":  "#a3a39b",
}
_ESTADO_LABEL = {
    "emitido":   "✅ Emitido",
    "rechazado": "⚠️ Rechazado",
    "anulado":   "✕ Anulado",
    "borrador":  "… Borrador",
}


def _cliente_de(row: dict) -> str:
    nombre = str(row.get("cliente_nombre") or "").strip()
    doc = str(row.get("cliente_doc") or "").strip()
    if nombre and doc:
        return f"{nombre} · {doc}"
    return nombre or doc or "—"


# ══════════════════════════════════════════════════════════════════════════════
# SECCIÓN: FACTURAS (ADMIN)
# ══════════════════════════════════════════════════════════════════════════════
def render():
    # Defensa en profundidad: el router solo crea esta pestaña para quien ve ingresos,
    # pero revalidamos por si se alcanza por una ruta inesperada.
    if not auth.can("see_revenue"):
        st.error("🔒 Acceso denegado")
        st.stop()

    st.markdown(titulo_seccion('🧾 Facturas · documentos fiscales emitidos'),
                unsafe_allow_html=True)

    docs = facturacion.documentos_recientes()
    if not docs:
        st.markdown(
            '<p style="color:#a3a39b; font-size:0.9rem; padding:1.5rem 0; text-align:center;">'
            'Aún no se ha emitido ningún documento fiscal.</p>',
            unsafe_allow_html=True,
        )
        return

    emitidos = [d for d in docs if d.get("estado") == "emitido"]
    rechazados = [d for d in docs if d.get("estado") == "rechazado"]
    monto_emitido = sum(int(d.get("monto") or 0) for d in emitidos)

    m1, m2, m3 = st.columns(3)
    with m1:
        st.markdown(f'<div class="metric-card"><div class="metric-value">{len(emitidos)}</div>'
                    '<div class="metric-label">Emitidos</div></div>', unsafe_allow_html=True)
    with m2:
        st.markdown('<div class="metric-card"><div class="metric-value" style="color:#dc2626;">'
                    f'{len(rechazados)}</div><div class="metric-label">Rechazados</div></div>',
                    unsafe_allow_html=True)
    with m3:
        st.markdown('<div class="metric-card"><div class="metric-value" '
                    f'style="font-size:clamp(0.9rem,1.8vw,2rem); white-space:nowrap;">'
                    f'${fmt_money(monto_emitido)}</div>'
                    '<div class="metric-label">Monto facturado</div></div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    for row in docs:
        _fila_documento(row)


def _fila_documento(row: dict) -> None:
    estado = str(row.get("estado") or "borrador")
    color = _ESTADO_COLOR.get(estado, "#a3a39b")
    etiqueta_estado = _ESTADO_LABEL.get(estado, estado)
    numero = str(row.get("numero") or "—")
    cufe = str(row.get("cufe") or "")
    cufe_corto = f"{cufe[:16]}…" if len(cufe) > 16 else cufe
    fecha = row.get("fecha")
    try:
        hora_txt = fecha.strftime("%d/%m %H:%M") if fecha else "—"
    except Exception:
        hora_txt = "—"
    tipo_txt = "Factura de venta" if row.get("tipo") == "factura" else "Doc. equivalente POS"
    cufe_html = f' · CUFE {html.escape(cufe_corto)}' if cufe_corto else ''

    st.markdown(f"""
    <div class="order-card" style="border-left:4px solid {color};">
      <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
          <div class="order-id">{html.escape(numero)} · {html.escape(tipo_txt)}</div>
          <div class="order-num">{html.escape(_cliente_de(row))}</div>
          <div style="font-size:0.78rem; color:#6b6b64; margin-top:2px;">
            {html.escape(hora_txt)} · {html.escape(str(row.get("proveedor") or "—"))}{cufe_html}
          </div>
        </div>
        <div style="text-align:right;">
          <span style="color:{color}; font-weight:700; font-size:0.85rem;">{etiqueta_estado}</span>
          <div class="order-total" style="margin-top:8px;">${fmt_money(row.get("monto") or 0)}</div>
        </div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    if estado != "emitido":
        return

    doc_id = int(row["id"])
    try:
        ids = json.loads(row.get("pedido_ids") or "[]")
    except (ValueError, TypeError):
        ids = []

    c1, c2 = st.columns(2)
    with c1:
        if ids and st.button("🖨️ Reimprimir", key=f"fact_reimp_{doc_id}",
                             use_container_width=True):
            pedidos.reimprimir_recibo(ids, numero, documento_fiscal={
                "estado": "emitido", "numero": row.get("numero"),
                "cufe": row.get("cufe"), "qr_texto": row.get("qr_texto"),
            })
            st.toast("Factura reimpresa", icon="🖨️")
    with c2:
        if st.button("✕ Anular", key=f"fact_anular_{doc_id}", use_container_width=True):
            st.session_state[f"_confirmar_anular_{doc_id}"] = True

    if st.session_state.get(f"_confirmar_anular_{doc_id}"):
        motivo = st.text_input("Motivo de la anulación", key=f"fact_motivo_{doc_id}")
        if st.button("Confirmar anulación", key=f"fact_anular_confirm_{doc_id}",
                     type="primary"):
            ok, msg = facturacion.anular_documento(doc_id, motivo)
            if ok:
                audit.registrar("factura_anulada", "documento_fiscal", doc_id,
                                {"numero": row.get("numero"), "motivo": motivo})
                st.session_state.pop(f"_confirmar_anular_{doc_id}", None)
                st.toast(msg, icon="✅")
                st.rerun()
            else:
                st.error(msg)
