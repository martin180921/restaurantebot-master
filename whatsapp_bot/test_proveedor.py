import os

os.environ.setdefault("WA_TOKEN", "test-token")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "123456123")

import proveedor  # noqa: E402  (env vars deben existir antes del import)


# ── Payloads de ejemplo, copiados de la documentación de Meta ──────────────
# https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples

PAYLOAD_MENSAJE_TEXTO = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
        "changes": [{
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {
                    "display_phone_number": "16505551111",
                    "phone_number_id": "123456123",
                },
                "contacts": [{
                    "profile": {"name": "NAME"},
                    "wa_id": "16315551181",
                }],
                "messages": [{
                    "from": "16315551181",
                    "id": "wamid.HBgLMTY1MDM3NjAxMDUVAgARGBI5QUY0RUM3RkYxQzYyRTBEMzUA",
                    "timestamp": "1603059201",
                    "text": {"body": "Hello this is an answer"},
                    "type": "text",
                }],
            },
            "field": "messages",
        }],
    }],
}

PAYLOAD_ESTADO = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
        "changes": [{
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {
                    "display_phone_number": "16505551111",
                    "phone_number_id": "123456123",
                },
                "statuses": [{
                    "id": "wamid.HBgLMTY0NjcwNDM1OTUVAgARGBI5QzhGMjRDRUY0ODEzM0Q4RUEA",
                    "status": "read",
                    "timestamp": "1603059201",
                    "recipient_id": "16315551181",
                }],
            },
            "field": "messages",
        }],
    }],
}

PAYLOAD_ECHO = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
        "changes": [{
            "field": "smb_message_echoes",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {
                    "display_phone_number": "16505551111",
                    "phone_number_id": "123456123",
                },
                "message_echoes": [{
                    "from": "16505551111",
                    "to": "16315551181",
                    "id": "wamid.HBgLMTY1MDM3NjAxMDUVAgARGBI5QUY0RUM3RkYxQzYyRTBEMzUA",
                    "timestamp": "1603059201",
                    "text": {"body": "Hello, how may I help you?"},
                    "type": "text",
                }],
            },
        }],
    }],
}

PAYLOAD_CONTACTO = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
        "changes": [{
            "field": "smb_app_state_sync",
            "value": {
                "messaging_product": "whatsapp",
                "state_sync": [{
                    "type": "contact",
                    "action": "add",
                    "contact": {
                        "full_name": "NAME",
                        "first_name": "NAME",
                        "phone_number": "16315551181",
                    },
                    "metadata": {"timestamp": "1603059201"},
                }],
            },
        }],
    }],
}

PAYLOAD_CUENTA = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
        "changes": [{
            "field": "account_update",
            "value": {
                "phone_number": "16505551111",
                "event": "PARTNER_REMOVED",
                "waba_info": {
                    "waba_id": "102290129340398",
                    "owner_business_id": "89737549839495",
                },
            },
        }],
    }],
}

PAYLOAD_FIELD_DESCONOCIDO = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
        "changes": [{
            "field": "algo_que_meta_agregue_manana",
            "value": {"lo_que_sea": True},
        }],
    }],
}


def test_mensaje_texto_entrante():
    eventos = proveedor.parsear_webhook(PAYLOAD_MENSAJE_TEXTO)
    assert len(eventos) == 1
    ev = eventos[0]
    assert ev.tipo == "entrante"
    assert ev.wamid == "wamid.HBgLMTY1MDM3NjAxMDUVAgARGBI5QUY0RUM3RkYxQzYyRTBEMzUA"
    assert ev.telefono == "16315551181"
    assert ev.texto == "Hello this is an answer"


def test_estado_usa_recipient_id():
    eventos = proveedor.parsear_webhook(PAYLOAD_ESTADO)
    assert len(eventos) == 1
    ev = eventos[0]
    assert ev.tipo == "estado"
    assert ev.wamid == "wamid.HBgLMTY0NjcwNDM1OTUVAgARGBI5QzhGMjRDRUY0ODEzM0Q4RUEA"
    assert ev.telefono == "16315551181"


def test_echo_el_cliente_es_el_to_no_el_from():
    eventos = proveedor.parsear_webhook(PAYLOAD_ECHO)
    assert len(eventos) == 1
    ev = eventos[0]
    assert ev.tipo == "echo"
    assert ev.wamid == "wamid.HBgLMTY1MDM3NjAxMDUVAgARGBI5QUY0RUM3RkYxQzYyRTBEMzUA"
    # El 'from' es el número del negocio (16505551111); el cliente es el 'to'.
    assert ev.telefono == "16315551181"
    assert ev.telefono != "16505551111"


def test_contacto_sincronizado():
    eventos = proveedor.parsear_webhook(PAYLOAD_CONTACTO)
    assert len(eventos) == 1
    ev = eventos[0]
    assert ev.tipo == "contacto"
    assert ev.telefono == "16315551181"
    assert ev.wamid is None


def test_cuenta_sin_telefono():
    eventos = proveedor.parsear_webhook(PAYLOAD_CUENTA)
    assert len(eventos) == 1
    ev = eventos[0]
    assert ev.tipo == "cuenta"
    assert ev.telefono is None
    assert ev.crudo["event"] == "PARTNER_REMOVED"


def test_field_desconocido_no_revienta():
    eventos = proveedor.parsear_webhook(PAYLOAD_FIELD_DESCONOCIDO)
    assert eventos == []


def test_payload_vacio_no_revienta():
    assert proveedor.parsear_webhook({}) == []
    assert proveedor.parsear_webhook({"entry": []}) == []


def test_payload_malformado_no_revienta():
    assert proveedor.parsear_webhook(None) == []
    assert proveedor.parsear_webhook({"entry": [{"changes": [{"field": "messages"}]}]}) == []
    assert proveedor.parsear_webhook({"entry": ["no-es-un-dict"]}) == []


def test_normalizar_tel_quita_mas_y_no_digitos():
    assert proveedor._normalizar_tel("+57 300 123 4567") == "573001234567"
    assert proveedor._normalizar_tel("whatsapp:+573001234567") == "573001234567"
    assert proveedor._normalizar_tel("") == ""
    assert proveedor._normalizar_tel(None) == ""


def test_enviar_texto_arma_payload_y_normaliza_destino(monkeypatch):
    llamada = {}

    class RespuestaFalsa:
        status_code = 200
        text = "{}"

    def post_falso(url, json, headers, timeout):
        llamada["url"] = url
        llamada["json"] = json
        llamada["headers"] = headers
        llamada["timeout"] = timeout
        return RespuestaFalsa()

    monkeypatch.setattr(proveedor, "WA_TOKEN", "test-token")
    monkeypatch.setattr(proveedor, "WA_PHONE_NUMBER_ID", "123456123")
    monkeypatch.setattr(proveedor, "WA_API_VERSION", "v23.0")
    monkeypatch.setattr(proveedor.requests, "post", post_falso)

    ok = proveedor.enviar_texto("+57 300 123 4567", "hola")

    assert ok is True
    assert llamada["url"] == "https://graph.facebook.com/v23.0/123456123/messages"
    assert llamada["headers"]["Authorization"] == "Bearer test-token"
    assert llamada["timeout"] == 10
    assert llamada["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "573001234567",
        "type": "text",
        "text": {"preview_url": True, "body": "hola"},
    }


def test_enviar_texto_nunca_propaga_excepciones(monkeypatch):
    def post_que_revienta(*args, **kwargs):
        raise ConnectionError("boom")

    monkeypatch.setattr(proveedor, "WA_TOKEN", "test-token")
    monkeypatch.setattr(proveedor, "WA_PHONE_NUMBER_ID", "123456123")
    monkeypatch.setattr(proveedor.requests, "post", post_que_revienta)

    assert proveedor.enviar_texto("573001234567", "hola") is False


def test_enviar_texto_sin_credenciales_devuelve_false(monkeypatch):
    monkeypatch.setattr(proveedor, "WA_TOKEN", "")
    assert proveedor.enviar_texto("573001234567", "hola") is False
