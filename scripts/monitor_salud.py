# -*- coding: utf-8 -*-
"""Chequeo de salud de la flota de restaurantes — puro Python (solo psycopg2 + stdlib).

    # Un restaurante suelto:
    python scripts/monitor_salud.py --database-url postgresql://...

    # Varios a la vez (config con la lista):
    python scripts/monitor_salud.py --config restaurantes_monitor.json

Qué revisa en cada base:
  1. Conectividad  — ¿responde la base?
  2. Agente de impresión — ¿latió hace poco? (agentes_estado.visto_at). Si lleva más de
     --umbral-min minutos sin latir, o nunca latió, es ALERTA: el local no está imprimiendo.
  3. Comandas falladas — print_jobs en estado 'error' (ALERTA: algo no salió por impresora).
  4. Cola atascada — print_jobs 'pendiente' con más de --umbral-min minutos (AVISO: puede que
     el agente esté caído o la impresora sin papel).

Salida: una línea por restaurante (OK / AVISO / ALERTA) y un resumen. Sale con código ≠ 0 si
hay al menos una ALERTA, para que una tarea programada dispare la notificación.

Notificación opcional a Telegram: si defines las variables de entorno TELEGRAM_BOT_TOKEN y
TELEGRAM_CHAT_ID y hay algún problema, manda el resumen por Telegram. Sin esas variables solo
imprime en consola (útil para probar sin configurar nada).
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

try:
    import psycopg2
except ImportError:
    sys.exit("[FATAL] Falta psycopg2. Instala: pip install psycopg2-binary")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

OK, AVISO, ALERTA = "OK", "AVISO", "ALERTA"


def _host_de(url: str) -> str:
    """host/bd sin credenciales, para mostrar sin filtrar la contraseña."""
    return url.split("@")[-1].split("?")[0]


def revisar(nombre: str, url: str, umbral_min: int) -> dict:
    """Revisa UNA base. Devuelve {nombre, nivel, host, notas:[...]}."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    r = {"nombre": nombre, "host": _host_de(url), "nivel": OK, "notas": []}
    # MISMA convención que el resto del sistema (db.py, agent.py): fijar la sesión en hora de
    # Bogotá. Sin esto, el agente escribe su latido en Bogotá pero este monitor restaría con
    # NOW() en UTC (default de Railway) → todo saldría ~5 h "viejo" y alertaría en falso.
    tz = "-c timezone=America/Bogota"

    def subir(nivel):
        orden = {OK: 0, AVISO: 1, ALERTA: 2}
        if orden[nivel] > orden[r["nivel"]]:
            r["nivel"] = nivel

    try:
        conn = psycopg2.connect(url, connect_timeout=10, options=tz)
    except Exception as exc:
        r["nivel"] = ALERTA
        r["notas"].append(f"sin conexión a la base: {str(exc).splitlines()[0][:120]}")
        return r

    try:
        with conn.cursor() as cur:
            # 2) Latido del agente de impresión.
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - visto_at)) FROM agentes_estado "
                "ORDER BY visto_at DESC LIMIT 1")
            fila = cur.fetchone()
            if fila is None:
                subir(ALERTA)
                r["notas"].append("el agente de impresión nunca ha latido")
            else:
                seg = int(fila[0] or 0)
                if seg > umbral_min * 60:
                    subir(ALERTA)
                    r["notas"].append(
                        f"agente sin latir hace {seg // 60} min (>{umbral_min})")

            # 3) Comandas falladas.
            cur.execute("SELECT COUNT(*) FROM print_jobs WHERE estado = 'error'")
            errores = cur.fetchone()[0]
            if errores:
                subir(ALERTA)
                r["notas"].append(f"{errores} comanda(s) en estado 'error'")

            # 4) Cola pendiente atascada.
            cur.execute(
                "SELECT COUNT(*) FROM print_jobs WHERE estado = 'pendiente' "
                "AND creado_at < NOW() - make_interval(mins => %s)", (umbral_min,))
            atascadas = cur.fetchone()[0]
            if atascadas:
                subir(AVISO)
                r["notas"].append(
                    f"{atascadas} comanda(s) pendiente(s) hace >{umbral_min} min")
    except Exception as exc:
        subir(AVISO)
        r["notas"].append(f"error al consultar: {str(exc).splitlines()[0][:120]}")
    finally:
        conn.close()

    if not r["notas"]:
        r["notas"].append("todo en orden")
    return r


def cargar_objetivos(args) -> list:
    """Lista de (nombre, url) desde --config y/o --database-url."""
    objetivos = []
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        for r in cfg.get("restaurantes", []):
            url = r.get("database_url")
            if url:
                objetivos.append((r.get("nombre") or _host_de(url), url))
    if args.database_url:
        objetivos.append((args.nombre or _host_de(args.database_url), args.database_url))
    return objetivos


def notificar_telegram(texto: str) -> None:
    """Push a Telegram si hay token+chat en el entorno; si no, no hace nada."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        datos = urllib.parse.urlencode({"chat_id": chat, "text": texto}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        with urllib.request.urlopen(urllib.request.Request(url, data=datos), timeout=15) as resp:
            resp.read()
        print("  (aviso enviado a Telegram)")
    except Exception as exc:
        print(f"  (no se pudo avisar por Telegram: {exc})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Chequeo de salud de la flota de restaurantes.")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                    help="postgresql://... de un restaurante (o variable DATABASE_URL)")
    ap.add_argument("--nombre", help="Etiqueta para el restaurante de --database-url")
    ap.add_argument("--config", help="JSON con {\"restaurantes\":[{\"nombre\",\"database_url\"}]}")
    ap.add_argument("--umbral-min", type=int, default=10,
                    help="Minutos sin latido/cola para alertar (def: 10)")
    args = ap.parse_args()

    objetivos = cargar_objetivos(args)
    if not objetivos:
        sys.exit("[FATAL] Da --database-url o --config con al menos un restaurante")

    resultados = [revisar(n, u, args.umbral_min) for n, u in objetivos]

    icono = {OK: "✅", AVISO: "🟡", ALERTA: "🔴"}
    lineas = []
    for r in resultados:
        lineas.append(f"{icono[r['nivel']]} {r['nombre']} ({r['host']}) — "
                      + "; ".join(r["notas"]))
    print("\n".join(lineas))

    n_alerta = sum(1 for r in resultados if r["nivel"] == ALERTA)
    n_aviso = sum(1 for r in resultados if r["nivel"] == AVISO)
    print(f"\nResumen: {len(resultados)} restaurante(s) · "
          f"{n_alerta} alerta(s) · {n_aviso} aviso(s)")

    if n_alerta or n_aviso:
        notificar_telegram("olo · salud de la flota\n" + "\n".join(lineas))

    sys.exit(1 if n_alerta else 0)


if __name__ == "__main__":
    main()
