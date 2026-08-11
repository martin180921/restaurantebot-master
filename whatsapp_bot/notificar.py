"""Aviso por Telegram, stdlib puro (sin requests). Compartido entre este bot
y scripts/monitor_salud.py para no mantener dos copias que acaben divergiendo
(una de las dos deja de avisar sin que nadie lo note).
"""
import os
import urllib.parse
import urllib.request


def notificar_telegram(texto: str) -> None:
    """Push a Telegram si hay TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID en el
    entorno; si no, no hace nada (útil para probar sin configurar nada).
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        datos = urllib.parse.urlencode({"chat_id": chat, "text": texto}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        with urllib.request.urlopen(urllib.request.Request(url, data=datos), timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"[WARN] No se pudo avisar por Telegram: {e}")
