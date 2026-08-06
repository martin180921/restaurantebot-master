"""Capa de proveedor: habla con el Cloud API de Meta (WhatsApp). Sin lógica de
negocio — no sabe qué es un pedido, ni cuándo saludar, ni qué es una pausa.
Superficie pública: Evento, enviar_texto(), parsear_webhook().
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import requests

WA_TOKEN           = os.getenv("WA_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
WA_API_VERSION     = os.getenv("WA_API_VERSION", "v23.0")

_TIMEOUT_S = 10


@dataclass
class Evento:
    tipo: str  # 'entrante' | 'echo' | 'contacto' | 'estado' | 'cuenta'
    wamid: Optional[str]
    telefono: Optional[str]
    texto: Optional[str]
    crudo: dict


def _normalizar_tel(telefono: Optional[str]) -> str:
    """Solo dígitos, con indicativo, sin '+'. Mismo helper al enviar y al
    parsear, para que la misma persona sea siempre la misma clave en la base.
    """
    if not telefono:
        return ""
    return re.sub(r"\D", "", telefono)


def enviar_texto(telefono: str, cuerpo: str) -> bool:
    """POST .../messages con un mensaje de texto. Nunca propaga excepciones:
    registra el error y devuelve False.
    """
    if not WA_TOKEN or not WA_PHONE_NUMBER_ID:
        print("[WARN] WA_TOKEN/WA_PHONE_NUMBER_ID no configurados; no se envió el mensaje.")
        return False

    url = f"https://graph.facebook.com/{WA_API_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _normalizar_tel(telefono),
        "type": "text",
        "text": {"preview_url": True, "body": cuerpo},
    }
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT_S)
        if resp.status_code >= 400:
            print(f"[ERROR] Cloud API respondió {resp.status_code}: {resp.text}")
            return False
        return True
    except Exception as e:
        print(f"[ERROR] No se pudo enviar el WhatsApp (Cloud API): {e}")
        return False


def _texto_de(mensaje: dict) -> Optional[str]:
    if mensaje.get("type") == "text":
        return (mensaje.get("text") or {}).get("body")
    return None


def _procesar_change(change: dict, eventos: list) -> None:
    campo = change.get("field")
    valor = change.get("value") or {}

    if campo == "messages":
        for msg in valor.get("messages") or []:
            eventos.append(Evento(
                tipo="entrante",
                wamid=msg.get("id"),
                telefono=_normalizar_tel(msg.get("from")),
                texto=_texto_de(msg),
                crudo=msg,
            ))
        for st in valor.get("statuses") or []:
            eventos.append(Evento(
                tipo="estado",
                wamid=st.get("id"),
                telefono=_normalizar_tel(st.get("recipient_id")),
                texto=None,
                crudo=st,
            ))
    elif campo == "smb_message_echoes":
        # El evento describe un mensaje que MANDÓ el restaurante: el 'from' es
        # el número del negocio, el CLIENTE es el 'to'.
        for eco in valor.get("message_echoes") or []:
            eventos.append(Evento(
                tipo="echo",
                wamid=eco.get("id"),
                telefono=_normalizar_tel(eco.get("to")),
                texto=_texto_de(eco),
                crudo=eco,
            ))
    elif campo == "smb_app_state_sync":
        for sync in valor.get("state_sync") or []:
            contacto = sync.get("contact") or {}
            eventos.append(Evento(
                tipo="contacto",
                wamid=None,
                telefono=_normalizar_tel(contacto.get("phone_number")),
                texto=None,
                crudo=sync,
            ))
    elif campo == "account_update":
        eventos.append(Evento(
            tipo="cuenta",
            wamid=None,
            telefono=None,
            texto=None,
            crudo=valor,
        ))
    # campo desconocido: se ignora, sin reventar.


def parsear_webhook(payload: dict) -> list[Evento]:
    """Aplana entry[].changes[] en una lista de Evento. Nunca lanza: un
    payload malformado o un field desconocido devuelven lo que se pudo
    parsear (lista vacía en el peor caso). Se llama desde un hilo de fondo,
    donde una excepción se pierde en silencio.
    """
    eventos: list[Evento] = []
    try:
        entradas = payload.get("entry") or []
    except AttributeError:
        return eventos

    for entrada in entradas:
        if not isinstance(entrada, dict):
            continue
        for change in entrada.get("changes") or []:
            if not isinstance(change, dict):
                continue
            try:
                _procesar_change(change, eventos)
            except Exception as e:
                print(f"[ERROR] No se pudo parsear un evento del webhook: {e}")

    return eventos
