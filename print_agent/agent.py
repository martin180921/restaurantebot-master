"""Agente de Impresión Local (corre DENTRO del restaurante, NO en Railway).

Hace polling a la cola `print_jobs` de la BD en la nube filtrando por el
RESTAURANTE_ID de este local, e imprime cada trabajo en la Epson 80mm conectada
al cajón monedero SAT. Aislado a propósito del código Streamlit: solo comparte la
tabla `print_jobs` como contrato.

Uso:
    cp config.example.json config.json   # y edítalo
    pip install -r requirements.txt
    python agent.py
"""
import argparse
import json
import os
import sys
import time
import signal
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor

# ── Config ───────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Ancho útil de una térmica de 80mm con fuente A: ~48 caracteres.
ANCHO = 48
# Pulso de apertura del cajón SAT (raw ESC p 0 25 150). Va al INICIO del buffer.
PULSO_CAJON = b"\x1b\x70\x00\x19\x96"


def cargar_config(requeridas=("DATABASE_URL", "RESTAURANTE_ID", "PRINTER_CONNECTION")) -> dict:
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"[FATAL] No existe {CONFIG_PATH}. Copia config.example.json y edítalo.")
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    for clave in requeridas:
        if clave not in cfg:
            sys.exit(f"[FATAL] Falta '{clave}' en config.json")
    return cfg


# ── Impresora ────────────────────────────────────────────────────────────────────
def abrir_impresora(conn_cfg: dict):
    """Crea el objeto python-escpos según el tipo de conexión (windows | usb | network).

    Se abre por trabajo (no se mantiene) para que un desconexión/reconexión del
    cable no deje el agente en un estado muerto: el siguiente intento reconecta.
    """
    # Import perezoso POR RAMA: así el módulo carga (y --dry-run funciona) sin escpos,
    # y en Linux/RasPi no intentamos importar Win32Raw (que requiere pywin32).
    tipo = conn_cfg.get("type", "usb").lower()
    try:
        if tipo in ("windows", "win32raw"):
            # Imprime por el SPOOLER de Windows usando el driver Epson ya instalado.
            # No requiere libusb/Zadig: es la vía recomendada en PC Windows. Si no se
            # da 'printer_name', usa la impresora predeterminada.
            from escpos.printer import Win32Raw
            nombre = conn_cfg.get("printer_name")
            return Win32Raw(nombre) if nombre else Win32Raw()
        if tipo == "network":
            from escpos.printer import Network
            return Network(conn_cfg["host"], port=int(conn_cfg.get("port", 9100)), timeout=10)
        if tipo == "usb":
            from escpos.printer import Usb
            return Usb(int(conn_cfg["vendor_id"], 16), int(conn_cfg["product_id"], 16))
    except ImportError as exc:
        sys.exit(f"[FATAL] Falta una dependencia para type='{tipo}' ({exc}). "
                 "Corre: pip install -r requirements.txt")
    raise ValueError(f"PRINTER_CONNECTION.type desconocido: {tipo!r}")


def fmt_money(valor) -> str:
    """35000 → '35.000' (miles con punto, estilo LATAM)."""
    try:
        return f"{int(round(float(valor))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def linea_precio(etiqueta: str, monto, ancho: int = ANCHO) -> str:
    """'Total                                      $35.000' (precio alineado a la derecha)."""
    derecha = f"${fmt_money(monto)}"
    espacio = max(1, ancho - len(etiqueta) - len(derecha))
    return f"{etiqueta}{' ' * espacio}{derecha}"


def imprimir_recibo(printer, payload: dict) -> None:
    """Compone y envía el ticket de 80mm. El cajón (si aplica) se abre primero."""
    # 1) Cajón SAT al inicio del buffer, ANTES de cualquier texto, si el cobro fue
    #    en efectivo. Lo manda el panel en el payload (abrir_cajon).
    if payload.get("abrir_cajon"):
        printer._raw(PULSO_CAJON)

    # 2) Encabezado.
    printer.set(align="center", bold=True, double_height=True, double_width=True)
    printer.text("RECIBO\n")
    printer.set(align="center", bold=False, double_height=False, double_width=False)
    mesa = payload.get("mesa") or "—"
    printer.text(f"{mesa}\n")
    printer.text(datetime.now().strftime("%d/%m/%Y  %H:%M") + "\n")
    printer.text("-" * ANCHO + "\n")

    # 3) Ítems (nombre en negrita, cantidad a la izquierda).
    printer.set(align="left")
    for it in payload.get("items", []):
        nombre = str(it.get("nombre", "?"))
        cant = int(it.get("cantidad", 1) or 1)
        printer.set(bold=True)
        printer.text(f"{cant:>2} x {nombre}\n")
        printer.set(bold=False)
    printer.text("-" * ANCHO + "\n")

    # 4) Totales y desglose de pago.
    printer.text(linea_precio("Total", payload.get("total", 0)) + "\n")
    printer.set(bold=True)
    metodo = str(payload.get("metodo", "")).capitalize()
    printer.text(linea_precio(f"Pagado ({metodo})", payload.get("pagado", 0)) + "\n")
    printer.set(bold=False)

    if payload.get("metodo") == "efectivo" and payload.get("recibido") is not None:
        printer.text(linea_precio("Recibido", payload.get("recibido", 0)) + "\n")
        printer.text(linea_precio("Cambio", payload.get("cambio", 0)) + "\n")

    saldo = int(payload.get("saldo", 0) or 0)
    if saldo > 0:
        printer.set(bold=True)
        printer.text(linea_precio("SALDO PENDIENTE", saldo) + "\n")
        printer.set(bold=False)
        printer.set(align="center")
        printer.text("** CUENTA AUN ABIERTA **\n")

    # 5) Pie + corte automático.
    printer.set(align="center")
    printer.text("\n¡Gracias!\n")
    printer.cut()


# ── Modo prueba (sin BD) ─────────────────────────────────────────────────────────
def _payload_demo() -> dict:
    """Payload de muestra con la MISMA forma que enqueue_recibo del panel. Efectivo
    con cambio y abrir_cajon=True, para validar de un tiro impresora + cajón."""
    return {
        "mesa": "Mesa 5 (PRUEBA)",
        "items": [
            {"nombre": "Pizza Margarita", "cantidad": 2},
            {"nombre": "Coca-Cola 350ml", "cantidad": 3},
            {"nombre": "Tiramisu", "cantidad": 1},
        ],
        "total": 78000,
        "pagado": 78000,
        "saldo": 0,
        "metodo": "efectivo",
        "recibido": 100000,
        "cambio": 22000,
        "abrir_cajon": True,
        "pedido_ids": [0],
    }


class _DummyPrinter:
    """Impresora simulada para --dry-run: acumula texto en vez de mandarlo al hardware,
    para previsualizar el layout de 80mm sin papel ni escpos instalado."""
    def __init__(self):
        self._buf = []

    def set(self, **_kw):
        pass  # el formato (negrita/centrado) no se ve en texto plano

    def text(self, t):
        self._buf.append(t)

    def _raw(self, data):
        self._buf.append(f"[RAW {data!r}  ← pulso de cajón]\n")

    def cut(self):
        self._buf.append("─" * ANCHO + "  ✂\n")

    def close(self):
        pass

    def render(self) -> str:
        return "".join(self._buf)


def modo_test(cfg: dict) -> int:
    """Imprime un recibo de muestra en la impresora REAL (sin tocar la BD)."""
    print("[test] abriendo impresora…")
    try:
        printer = abrir_impresora(cfg["PRINTER_CONNECTION"])
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[test] NO se pudo abrir la impresora: {exc}")
        return 1
    try:
        print("[test] imprimiendo recibo de muestra (abrir_cajon=True)…")
        imprimir_recibo(printer, _payload_demo())
        try:
            printer.close()
        except Exception:
            pass
        print("[test] OK · revisa el ticket y que el cajón haya abierto.")
        return 0
    except Exception as exc:
        print(f"[test] FALLÓ la impresión: {exc}")
        return 1


def modo_dry_run() -> int:
    """Renderiza el recibo de muestra como TEXTO en consola. No usa impresora ni BD."""
    dummy = _DummyPrinter()
    imprimir_recibo(dummy, _payload_demo())
    print("┌" + "─" * ANCHO + "┐")
    print(dummy.render(), end="")
    print("└" + "─" * ANCHO + "┘")
    return 0


def modo_list_printers() -> int:
    """Lista las impresoras instaladas en Windows para copiar el 'printer_name' exacto
    a config.json (modo 'windows'). Solo aplica en Windows."""
    if sys.platform != "win32":
        print("[list-printers] Solo disponible en Windows. En Linux/macOS usa el modo "
              "'usb' o 'network' (ver README), o 'lpstat -p' para ver colas CUPS.")
        return 1
    # Import perezoso: pywin32 solo existe en Windows (ver requirements.txt).
    try:
        import win32print
    except ImportError:
        sys.exit("[FATAL] Falta pywin32. Corre: pip install -r requirements.txt")
    # Combinamos impresoras locales y conexiones de red mapeadas.
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    impresoras = win32print.EnumPrinters(flags)
    if not impresoras:
        print("[list-printers] No se encontraron impresoras instaladas en Windows.")
        return 1
    print("Impresoras de Windows (copia el nombre EXACTO a config.json → printer_name):")
    for i, p in enumerate(impresoras, 1):
        print(f"  {i}. {p[2]}")  # nivel 1: índice 2 = nombre de la impresora
    return 0


# ── Cola (claim atómico + cierre de estado) ──────────────────────────────────────
def reclamar_trabajo(conn, restaurante_id: int):
    """Toma UN trabajo 'pendiente' y lo pasa a 'imprimiendo' atómicamente.

    FOR UPDATE SKIP LOCKED evita que dos agentes (o dos hilos) impriman el mismo
    ticket. Incrementa 'intentos'. Devuelve {id, payload} o None.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE print_jobs SET estado = 'imprimiendo', intentos = intentos + 1
            WHERE id = (
                SELECT id FROM print_jobs
                WHERE restaurante_id = %s AND estado = 'pendiente'
                ORDER BY creado_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, payload
            """,
            (restaurante_id,),
        )
        fila = cur.fetchone()
    conn.commit()
    return fila


def marcar_impreso(conn, job_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE print_jobs SET estado = 'impreso', impreso_at = NOW(), error_msg = NULL "
            "WHERE id = %s",
            (job_id,),
        )
    conn.commit()


def marcar_error(conn, job_id: int, mensaje: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE print_jobs SET estado = 'error', error_msg = %s WHERE id = %s",
            (mensaje[:1000], job_id),
        )
    conn.commit()


# ── Loop principal ───────────────────────────────────────────────────────────────
_corriendo = True


def _parar(*_):
    global _corriendo
    _corriendo = False
    print("\n[agent] señal recibida, cerrando…")


def main() -> None:
    # Consola Windows: por defecto usa cp1252 y revienta (UnicodeEncodeError) con
    # acentos o box-drawing. Forzamos UTF-8 en la salida para logs y --dry-run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Agente de impresión local (cola print_jobs).")
    parser.add_argument("--test", action="store_true",
                        help="Imprime un recibo de muestra en la impresora real y sale (no toca la BD).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Muestra el recibo de muestra como texto en consola (sin impresora ni BD).")
    parser.add_argument("--list-printers", action="store_true",
                        help="Lista las impresoras de Windows (para hallar printer_name) y sale.")
    args = parser.parse_args()

    if args.list_printers:
        sys.exit(modo_list_printers())
    if args.dry_run:
        sys.exit(modo_dry_run())
    if args.test:
        # Solo necesitamos la sección de impresora para validar el hardware.
        sys.exit(modo_test(cargar_config(requeridas=("PRINTER_CONNECTION",))))

    cfg = cargar_config()
    restaurante_id = int(cfg["RESTAURANTE_ID"])
    poll = float(cfg.get("POLL_SECONDS", 2))
    printer_cfg = cfg["PRINTER_CONNECTION"]

    signal.signal(signal.SIGINT, _parar)
    signal.signal(signal.SIGTERM, _parar)

    print(f"[agent] iniciado · restaurante_id={restaurante_id} · poll={poll}s")
    conn = None
    while _corriendo:
        try:
            if conn is None or conn.closed:
                conn = psycopg2.connect(cfg["DATABASE_URL"])
            trabajo = reclamar_trabajo(conn, restaurante_id)
        except Exception as exc:  # caída de BD → reintentar tras el sleep
            print(f"[agent] error de BD: {exc}")
            conn = None
            time.sleep(poll)
            continue

        if not trabajo:
            time.sleep(poll)
            continue

        job_id = trabajo["id"]
        payload = trabajo["payload"]
        if isinstance(payload, str):  # por si el driver no deserializa el JSONB
            payload = json.loads(payload)

        print(f"[agent] imprimiendo job #{job_id} (cajon={payload.get('abrir_cajon')})")
        try:
            printer = abrir_impresora(printer_cfg)
            imprimir_recibo(printer, payload)
            try:
                printer.close()
            except Exception:
                pass
            marcar_impreso(conn, job_id)
            print(f"[agent] job #{job_id} OK")
        except Exception as exc:
            # Impresora desconectada / sin papel / etc. → flag 'error' + log.
            print(f"[agent] job #{job_id} FALLÓ: {exc}")
            try:
                marcar_error(conn, job_id, str(exc))
            except Exception as exc2:
                print(f"[agent] no se pudo marcar error en BD: {exc2}")
                conn = None

    if conn and not conn.closed:
        conn.close()
    print("[agent] detenido.")


if __name__ == "__main__":
    main()
