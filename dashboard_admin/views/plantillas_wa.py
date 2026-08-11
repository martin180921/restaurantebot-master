"""Vista de Plantillas de WhatsApp (SOLO admin) — peaje del App Review de Meta.

El bot de OKU NO usa plantillas: solo responde dentro de la ventana de 24 h que abre el
cliente. Esta pantalla existe porque el App Review de Meta exige un video de la propia
app *creando una plantilla de mensaje* para aprobar la cuenta como Tech Provider — sin
eso no hay Coexistence. Es lo mínimo que pasa el review, no un editor de plantillas: sin
variables, botones, encabezados, media, envío ni versionado. Ver
docs/plan_whatsapp_coexistence.md, sección "P8".

Vive como pestaña dentro de Administración (mismo patrón que Facturas): oculta para caja
y meseros, sin capacidad propia en auth._CAPS porque no encaja en ninguna de las
existentes (no es de ingresos ni de menú) — se valida por rol directamente.
"""
import re

import requests
import streamlit as st

import auth
from db import titulo_seccion

WA_TOKEN = None
WA_API_VERSION = None
WA_BUSINESS_ACCOUNT_ID = None


def _config():
    """Lee la config en cada llamada (no al importar): permite setear las variables
    de entorno después de que Streamlit ya cargó el módulo, igual que hace el resto
    del panel con os.getenv en caliente."""
    import os
    return (
        os.getenv("WA_TOKEN", ""),
        os.getenv("WA_API_VERSION", "v23.0"),
        os.getenv("WA_BUSINESS_ACCOUNT_ID", ""),
    )


@st.cache_data(ttl=30, show_spinner=False)
def _listar_plantillas(token: str, api_version: str, waba_id: str) -> tuple:
    """(ok, datos_o_error). No propaga excepciones: un fallo de red no debe tumbar
    la pestaña."""
    url = f"https://graph.facebook.com/{api_version}/{waba_id}/message_templates"
    try:
        resp = requests.get(
            url, headers={"Authorization": f"Bearer {token}"},
            params={"fields": "name,language,category,status"}, timeout=15,
        )
        if resp.status_code >= 400:
            return False, f"Meta respondió {resp.status_code}: {resp.text[:300]}"
        return True, resp.json().get("data") or []
    except Exception as e:
        return False, str(e)


def _crear_plantilla(token: str, api_version: str, waba_id: str, nombre: str, cuerpo: str):
    """(ok, mensaje)."""
    url = f"https://graph.facebook.com/{api_version}/{waba_id}/message_templates"
    payload = {
        "name": nombre,
        "language": "es",
        "category": "UTILITY",
        "components": [{"type": "BODY", "text": cuerpo}],
    }
    try:
        resp = requests.post(
            url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=15,
        )
        if resp.status_code >= 400:
            return False, f"Meta respondió {resp.status_code}: {resp.text[:300]}"
        return True, "Plantilla creada."
    except Exception as e:
        return False, str(e)


_ESTADO_LABEL = {
    "APPROVED": "✅ Aprobada",
    "PENDING":  "⏳ Pendiente",
    "REJECTED": "❌ Rechazada",
}


def render():
    # Defensa en profundidad: el router solo crea esta pestaña dentro de 'admin',
    # ya validado por rol, pero se revalida por si se alcanza por una ruta inesperada.
    if auth.current_role() != auth.ADMIN:
        st.error("🔒 Acceso denegado")
        st.stop()

    st.markdown(titulo_seccion("🧩 Plantillas de WhatsApp"), unsafe_allow_html=True)
    st.caption(
        "El bot no manda plantillas — solo responde dentro de las 24 h que abre el "
        "cliente. Esta pantalla existe solo para el trámite de aprobación de Meta."
    )

    token, api_version, waba_id = _config()
    if not token or not waba_id:
        st.warning(
            "Faltan las variables de entorno `WA_TOKEN` y/o `WA_BUSINESS_ACCOUNT_ID` "
            "en este servicio. Sin ellas no se puede listar ni crear plantillas."
        )
        return

    if st.button("🔄 Actualizar lista", key="btn_plantillas_refrescar"):
        _listar_plantillas.clear()

    ok, datos = _listar_plantillas(token, api_version, waba_id)
    if not ok:
        st.error(f"No se pudo listar plantillas: {datos}")
    elif not datos:
        st.markdown(
            '<p style="color:#a3a39b; font-size:0.9rem; padding:1rem 0;">'
            'Todavía no hay ninguna plantilla creada.</p>', unsafe_allow_html=True)
    else:
        for pl in datos:
            estado = _ESTADO_LABEL.get(pl.get("status"), pl.get("status") or "—")
            st.markdown(
                f"**{pl.get('name', '—')}** · {pl.get('language', '—')} · "
                f"{pl.get('category', '—')} · {estado}"
            )

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-title">Crear plantilla</div>', unsafe_allow_html=True)
    st.caption("Idioma: español · Categoría: Utilidad (fijos — es lo único que acepta este trámite).")

    with st.form("form_crear_plantilla", clear_on_submit=True):
        nombre_in = st.text_input(
            "Nombre de la plantilla", key="pl_nombre",
            help="Solo minúsculas, números y guion bajo. Se ajusta automáticamente.")
        cuerpo_in = st.text_area(
            "Texto del mensaje", key="pl_cuerpo", height=120,
            help="El texto que verá el cliente cuando esta plantilla se envíe.")
        enviar = st.form_submit_button("➕ Crear plantilla", type="primary")

    if enviar:
        nombre_limpio = re.sub(r"[^a-z0-9_]", "_", nombre_in.strip().lower())[:512]
        if not nombre_limpio or not (cuerpo_in or "").strip():
            st.error("Nombre y texto del mensaje son obligatorios.")
        else:
            ok, msg = _crear_plantilla(token, api_version, waba_id, nombre_limpio, cuerpo_in.strip())
            if ok:
                _listar_plantillas.clear()
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)
