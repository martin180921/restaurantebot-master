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
import socket
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
# Fuente B (ESC/POS): más pequeña y ESTRECHA. En una térmica de 80mm caben ~64
# caracteres. Se usa solo en el CUERPO del recibo/prerecibo del cliente (ítems,
# totales, pago) para gastar menos papel y que no se vea abultado; el ancho cambia a
# ANCHO_B para que separadores y precios alineados a la derecha lleguen al borde.
ANCHO_B = 64
# Pulso de apertura del cajón SAT (raw ESC p 0 25 150). Va al INICIO del buffer.
PULSO_CAJON = b"\x1b\x70\x00\x19\x96"

# Cabeceras de categoría del ticket. Mismo contrato que el panel (utils/items.py);
# se duplica aquí a propósito: el agente es un proceso aislado que solo comparte la
# forma del payload de print_jobs, no el código Streamlit.
CAT_LABEL = {
    "plato_dia": "PLATO DEL DIA",
    "especial":  "ESPECIALES",
    "item":      "A LA CARTA",
    "adicional": "ADICIONALES",
    "bebida":    "BEBIDAS",
}

# Tipos que cuentan en el resumen "PLATOS Y BEBIDAS: N" de la cabecera del ticket.
# Excluye 'adicional' a propósito (los adicionales son extras, no platos). Mismo criterio
# que usa el panel para el conteo; se duplica aquí porque el agente es un proceso aislado.
DISH_TIPOS = ("plato_dia", "especial", "item", "bebida")


def _contar_platos(items) -> int:
    """Nº de unidades de platos + bebidas (sin adicionales) de un ticket, para el resumen
    de cantidad. Tolerante: un item sin 'tipo' cae en 'item' (a la carta) y cuenta."""
    total = 0
    for it in (items or []):
        if str(it.get("tipo") or "item").lower() in DISH_TIPOS:
            try:
                total += int(it.get("cantidad", 1) or 1)
            except (TypeError, ValueError):
                total += 1
    return total


def _texto_atendio(printer, payload: dict) -> None:
    """Imprime 'Atendio: <empleado>' si el payload trae 'mesero' (quién tomó el pedido).
    Ausente en pedidos armados por el cliente (app pública / QR) → no imprime nada."""
    mesero = str(payload.get("mesero") or "").strip()
    if mesero:
        printer.text(f"Atendió: {mesero}\n")


# Etiqueta del tipo de entrega para el bloque de contacto del ticket.
ENTREGA_LABEL = {"domicilio": "DOMICILIO", "para_llevar": "PARA LLEVAR"}


def _imprimir_contacto(printer, payload: dict) -> None:
    """Bloque de entrega: tipo (DOMICILIO / PARA LLEVAR) + teléfono + dirección del cliente,
    cuando el pedido es de entrega. En pedidos de mesa/QR dine-in no trae estos campos →
    no imprime nada. El repartidor necesita la dirección en el ticket físico, no solo en
    pantalla. El llamador decide el énfasis (negrita) y dónde va dentro del encabezado."""
    etiqueta = ENTREGA_LABEL.get(str(payload.get("tipo_entrega") or "").lower())
    tel = str(payload.get("telefono") or "").strip()
    direccion = str(payload.get("direccion") or "").strip()
    if not (etiqueta or tel or direccion):
        return
    if etiqueta:
        printer.text(f"** {etiqueta} **\n")
    if tel:
        printer.text(f"Tel: {tel}\n")
    if direccion:
        printer.text(f"Dir: {direccion}\n")

# Billeteras de transferencia (submetodo del payload → etiqueta legible en el ticket).
SUBMETODO_LABEL = {
    "nequi":     "Nequi",
    "daviplata": "Daviplata",
    "breb":      "Bre-B",
}


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


def _imprimir_items(printer, items, grande: bool = False) -> None:
    """Imprime los ítems AGRUPADOS por categoría ([PLATO DEL DIA], [BEBIDAS], …) con
    sus componentes indentados debajo (Entrada / Principio / Proteína / Acompañamientos
    / Nota). Retro-compatible: un item sin 'tipo' se imprime como 'N x nombre' sin
    cabecera ni desglose. 'grande' usa doble alto en el nombre (comanda de cocina)."""
    tipo_actual = None
    for it in items:
        nombre = str(it.get("nombre", "?"))
        cant = int(it.get("cantidad", 1) or 1)
        tipo = it.get("tipo")
        comps = it.get("componentes") or []
        # Cabecera de categoría al cambiar de tipo (solo si el item lo trae).
        if tipo and tipo != tipo_actual:
            printer.set(align="left", bold=True, double_height=False, double_width=False)
            printer.text(f"[{CAT_LABEL.get(str(tipo).lower(), str(tipo).upper())}]\n")
            tipo_actual = tipo
        printer.set(align="left", bold=True, double_height=grande, double_width=False)
        printer.text(f"{cant} x {nombre}\n")
        printer.set(bold=False, double_height=False)
        for par in comps:
            try:
                etiqueta, valor = par[0], par[1]
            except (IndexError, TypeError, KeyError):
                continue
            printer.text(f"   * {etiqueta}: {valor}\n")


def imprimir_recibo(printer, payload: dict) -> None:
    """Compone y envía el ticket de 80mm. El cajón (si aplica) se abre primero."""
    # 1) Cajón SAT al inicio del buffer, ANTES de cualquier texto, si el cobro fue
    #    en efectivo. Lo manda el panel en el payload (abrir_cajon).
    if payload.get("abrir_cajon"):
        printer._raw(PULSO_CAJON)

    # 2) Encabezado.
    printer.set(font="a", align="center", bold=True, double_height=True, double_width=True)
    printer.text("RECIBO\n")
    printer.set(align="center", bold=False, double_height=False, double_width=False)
    mesa = payload.get("mesa") or "—"
    printer.text(f"{mesa}\n")
    printer.text(datetime.now().strftime("%d/%m/%Y  %H:%M") + "\n")
    _texto_atendio(printer, payload)   # empleado que tomó el pedido (si lo hay)
    printer.set(align="left")
    _imprimir_contacto(printer, payload)   # tipo + tel + dirección si es entrega
    printer.text("-" * ANCHO + "\n")

    # 3) Cuerpo en FUENTE B (más pequeña/estrecha): ítems, totales y pago. Ahorra papel
    #    y evita el aspecto abultado del recibo del cliente. Es "pegajosa": en escpos>=3.1
    #    set(font=...) solo emite el comando cuando se pasa font, así que los set() de
    #    _imprimir_items (que no lo pasan) NO la revierten. El cuerpo usa ANCHO_B.
    printer.set(font="b")
    # Resumen de cantidad ANTES del detalle: cuántos platos+bebidas lleva el pedido.
    # align="left" explícito: la cabecera venía centrada y los ítems van a la izquierda.
    printer.set(align="left", bold=True)
    printer.text(f"PLATOS Y BEBIDAS: {_contar_platos(payload.get('items', []))}\n")
    printer.set(bold=False)
    _imprimir_items(printer, payload.get("items", []), grande=False)
    printer.text("-" * ANCHO_B + "\n")

    # 4) Totales y desglose de pago.
    printer.text(linea_precio("Total", payload.get("total", 0), ANCHO_B) + "\n")
    # Pago MIXTO (payload['desglose']): una sola persona pagó esta cuenta repartiendo
    # el monto entre efectivo y transferencia → imprimimos el total pagado y debajo cada
    # tramo, en UN solo ticket. En un cobro de método único mantenemos la línea de siempre.
    desglose = payload.get("desglose") or []
    if desglose:
        printer.set(bold=True)
        printer.text(linea_precio("Pagado (Mixto)", payload.get("pagado", 0), ANCHO_B) + "\n")
        printer.set(bold=False)
        for tramo in desglose:
            etiqueta = str(tramo.get("metodo", "")).capitalize()
            if tramo.get("metodo") == "transferencia":
                sub = SUBMETODO_LABEL.get(str(tramo.get("submetodo") or "").lower())
                if sub:
                    etiqueta = f"{etiqueta} · {sub}"
            printer.text(linea_precio(f"  {etiqueta}", tramo.get("monto", 0), ANCHO_B) + "\n")
            comp_t = str(tramo.get("comprobante") or "").strip()
            if tramo.get("metodo") == "transferencia" and comp_t:
                printer.text(f"  Comp. {comp_t}\n")
            # Tender del tramo en efectivo (Recibido/Cambio), si vino.
            if tramo.get("metodo") == "efectivo" and tramo.get("recibido") is not None:
                printer.text(linea_precio("  Recibido", tramo.get("recibido", 0), ANCHO_B) + "\n")
                printer.text(linea_precio("  Cambio", tramo.get("cambio", 0), ANCHO_B) + "\n")
    else:
        printer.set(bold=True)
        # En transferencia, anexa la billetera (Nequi/Daviplata/Bre-B) a la etiqueta del
        # método: 'Pagado (Transferencia · Nequi)'. En efectivo queda 'Pagado (Efectivo)'.
        metodo = str(payload.get("metodo", "")).capitalize()
        if payload.get("metodo") == "transferencia":
            sub = SUBMETODO_LABEL.get(str(payload.get("submetodo") or "").lower())
            if sub:
                metodo = f"{metodo} · {sub}"
        printer.text(linea_precio(f"Pagado ({metodo})", payload.get("pagado", 0), ANCHO_B) + "\n")
        printer.set(bold=False)

        # Comprobante de la transferencia (n.º de transacción), si se registró.
        comprobante = str(payload.get("comprobante") or "").strip()
        if payload.get("metodo") == "transferencia" and comprobante:
            printer.text(f"Comp. {comprobante}\n")

        if payload.get("metodo") == "efectivo" and payload.get("recibido") is not None:
            printer.text(linea_precio("Recibido", payload.get("recibido", 0), ANCHO_B) + "\n")
            printer.text(linea_precio("Cambio", payload.get("cambio", 0), ANCHO_B) + "\n")

    saldo = int(payload.get("saldo", 0) or 0)
    if saldo > 0:
        printer.set(bold=True)
        printer.text(linea_precio("SALDO PENDIENTE", saldo, ANCHO_B) + "\n")
        printer.set(bold=False)
        printer.set(align="center")
        printer.text("** CUENTA AUN ABIERTA **\n")

    # 5) Pie + corte automático.
    printer.set(align="center")
    printer.text("\n¡Gracias!\n")
    printer.cut()
    # La Fuente B solo aplica al recibo del cliente. Tras el corte volvemos a Fuente A
    # para que las siguientes comandas de cocina / reportes salgan en el tamaño legible
    # estándar (la impresora conserva la fuente entre trabajos hasta que se cambie).
    printer.set(font="a")


def imprimir_comanda(printer, payload: dict) -> None:
    """Ticket de COCINA: mesa, hora e ítems en grande. Sin precios ni cajón — la
    cocina solo necesita qué preparar y para quién."""
    printer.set(font="a", align="center", bold=True, double_height=True, double_width=True)
    printer.text("COMANDA\n")
    printer.set(align="center", bold=False, double_height=False, double_width=False)
    printer.text(f"{payload.get('mesa') or '—'}\n")
    printer.text(datetime.now().strftime("%d/%m/%Y  %H:%M") + "\n")
    _texto_atendio(printer, payload)   # empleado que tomó el pedido (si lo hay)
    # Datos de entrega EN NEGRITA: el repartidor lee la dirección del ticket de cocina.
    printer.set(align="left", bold=True)
    _imprimir_contacto(printer, payload)   # tipo + tel + dirección si es entrega
    printer.set(bold=False)
    printer.text("-" * ANCHO + "\n")
    # Resumen de cantidad ANTES del detalle: cuántos platos+bebidas hay que preparar.
    printer.set(align="left", bold=True)
    printer.text(f"PLATOS Y BEBIDAS: {_contar_platos(payload.get('items', []))}\n")
    printer.set(bold=False)
    # Ítems grandes (doble alto) para leerse de lejos; componentes en tamaño normal.
    _imprimir_items(printer, payload.get("items", []), grande=True)
    # Nota general del pedido (cambio de último momento, alergia…): destacada y en grande
    # para que la cocina no la pase por alto. Vacía/ausente → no se imprime.
    nota = str(payload.get("nota") or "").strip()
    if nota:
        printer.text("-" * ANCHO + "\n")
        printer.set(bold=True, double_height=True)
        printer.text(f"NOTA: {nota}\n")
        printer.set(bold=False, double_height=False)
    printer.text("-" * ANCHO + "\n")
    printer.cut()


def imprimir_prerecibo(printer, payload: dict) -> None:
    """PRERECIBO (pre-cuenta) para el cliente ANTES de pagar — botón "🖨 Ticket".

    Mismo layout que el recibo (mismos ítems en Fuente B + Total) pero con dos
    diferencias clave: el encabezado dice PRERECIBO con el aviso de que NO es una
    factura válida, y muestra de forma prominente la MESA de la que proviene la cuenta.
    Sin pulso de cajón ni desglose de pago: es una cuenta previa para revisar."""
    # 1) Encabezado PRERECIBO + aviso de que NO es factura (Fuente A, bien visible).
    printer.set(font="a", align="center", bold=True, double_height=True, double_width=True)
    printer.text("PRERECIBO\n")
    printer.set(align="center", bold=False, double_height=False, double_width=False)
    printer.text("** NO ES FACTURA VALIDA **\n")
    # 2) MESA siempre visible (requisito): de qué mesa proviene la cuenta.
    printer.set(bold=True)
    printer.text(f"Mesa: {payload.get('mesa') or '—'}\n")
    printer.set(bold=False)
    printer.text(datetime.now().strftime("%d/%m/%Y  %H:%M") + "\n")
    printer.text("-" * ANCHO + "\n")

    # 3) Cuerpo en FUENTE B (igual que el recibo): ítems + total.
    printer.set(font="b")
    _imprimir_items(printer, payload.get("items", []), grande=False)
    printer.text("-" * ANCHO_B + "\n")
    printer.text(linea_precio("Total", payload.get("total", 0), ANCHO_B) + "\n")

    # 4) Abonos previos (cuenta parcialmente pagada) → saldo pendiente.
    abonado = int(payload.get("pagado", 0) or 0)
    total = int(payload.get("total", 0) or 0)
    if abonado > 0:
        printer.text(linea_precio("Abonado", abonado, ANCHO_B) + "\n")
        printer.set(bold=True)
        printer.text(linea_precio("SALDO", max(0, total - abonado), ANCHO_B) + "\n")
        printer.set(bold=False)

    # 5) Pie + corte. Vuelve a Fuente A para no afectar comandas/reportes posteriores.
    printer.set(align="center")
    printer.text("\nCuenta previa - solicite su factura al pagar.\n")
    printer.cut()
    printer.set(font="a")


# ── Modo prueba (sin BD) ─────────────────────────────────────────────────────────
def _payload_demo() -> dict:
    """Payload de muestra con la MISMA forma que enqueue_recibo del panel. Efectivo
    con cambio y abrir_cajon=True, para validar de un tiro impresora + cajón."""
    return {
        "mesa": "Ana (PRUEBA)",
        "mesero": "Carlos",
        "tipo_entrega": "domicilio",
        "telefono": "300 123 4567",
        "direccion": "Calle 10 # 5-23, Barrio Centro, apto 302",
        "items": [
            {"tipo": "plato_dia", "nombre": "Plato del Día", "cantidad": 1,
             "componentes": [["Entrada", "Sopa de Lentejas"], ["Principio", "Frijol"],
                             ["Proteína", "Res"], ["Acompañamientos", "2x Arroz, 1x Maduro"],
                             ["Nota", "Sin ensalada"]]},
            {"tipo": "especial", "nombre": "Bisteck a caballo", "cantidad": 1, "componentes": []},
            {"tipo": "bebida", "nombre": "Coca-Cola 350ml", "cantidad": 3, "componentes": []},
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


def _payload_demo_transfer() -> dict:
    """Recibo de muestra pagado por TRANSFERENCIA (Nequi) con comprobante, para
    previsualizar la billetera y el n.º de transacción en el ticket. Sin cajón."""
    return {
        "mesa": "Mesa 4",
        "mesero": "Lucía",
        "items": [
            {"tipo": "especial", "nombre": "Bandeja Paisa", "cantidad": 2, "componentes": []},
            {"tipo": "bebida", "nombre": "Jugo de Mora", "cantidad": 2, "componentes": []},
        ],
        "total": 64000,
        "pagado": 64000,
        "saldo": 0,
        "metodo": "transferencia",
        "submetodo": "nequi",
        "comprobante": "M1234567890",
        "recibido": None,
        "cambio": 0,
        "abrir_cajon": False,
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


def _payload_demo_comanda() -> dict:
    """Comanda de muestra (misma forma que enqueue_comanda): sin precios ni cajón."""
    return {
        "pedido_id": 0,
        "mesa": "Ana (PRUEBA)",
        "mesero": "Carlos",
        "tipo_entrega": "domicilio",
        "telefono": "300 123 4567",
        "direccion": "Calle 10 # 5-23, Barrio Centro, apto 302",
        "items": [
            {"tipo": "plato_dia", "nombre": "Plato del Día", "cantidad": 1,
             "componentes": [["Entrada", "Sopa de Lentejas"], ["Principio", "Frijol"],
                             ["Proteína", "Res"], ["Acompañamientos", "2x Arroz, 1x Maduro"],
                             ["Nota", "Sin ensalada"]]},
            {"tipo": "especial", "nombre": "Bisteck a caballo", "cantidad": 1, "componentes": []},
            {"tipo": "bebida", "nombre": "Coca-Cola 350ml", "cantidad": 3, "componentes": []},
        ],
        "nota": "Cambio: ahora es PARA LLEVAR",
        "abrir_cajon": False,
    }


def _payload_demo_prerecibo() -> dict:
    """Prerecibo de muestra (misma forma que enqueue_prerecibo): mesa + ítems + total,
    parcialmente abonado para previsualizar el saldo. Sin método de pago ni cajón."""
    return {
        "pedido_id": 0,
        "mesa": "Mesa 4",
        "items": [
            {"tipo": "especial", "nombre": "Bandeja Paisa", "cantidad": 2, "componentes": []},
            {"tipo": "bebida", "nombre": "Jugo de Mora", "cantidad": 2, "componentes": []},
        ],
        "total": 64000,
        "pagado": 20000,
        "abrir_cajon": False,
    }


def modo_dry_run() -> int:
    """Renderiza recibo, prerecibo y comanda de muestra como TEXTO (sin impresora ni BD)."""
    for payload, render_fn in ((_payload_demo(), imprimir_recibo),
                               (_payload_demo_transfer(), imprimir_recibo),
                               (_payload_demo_prerecibo(), imprimir_prerecibo),
                               (_payload_demo_comanda(), imprimir_comanda)):
        dummy = _DummyPrinter()
        render_fn(dummy, payload)
        print("┌" + "─" * ANCHO + "┐")
        print(dummy.render(), end="")
        print("└" + "─" * ANCHO + "┘")
    return 0


def modo_status(cfg: dict) -> int:
    """Muestra el conteo de la cola por estado y los últimos errores. Solo lee la BD."""
    import psycopg2
    rid = int(cfg["RESTAURANTE_ID"])
    try:
        conn = psycopg2.connect(cfg["DATABASE_URL"])
    except Exception as exc:
        print(f"[status] no se pudo conectar a la BD: {exc}")
        return 1
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT estado, COUNT(*) FROM print_jobs WHERE restaurante_id=%s "
                        "GROUP BY estado ORDER BY estado", (rid,))
            filas = cur.fetchall()
            cur.execute("SELECT id, tipo, error_msg, creado_at FROM print_jobs "
                        "WHERE restaurante_id=%s AND estado='error' ORDER BY id DESC LIMIT 5", (rid,))
            errores = cur.fetchall()
    finally:
        conn.close()
    print(f"Cola print_jobs · restaurante_id={rid}")
    if not filas:
        print("  (sin trabajos)")
    for estado, n in filas:
        print(f"  {estado:<12} {n}")
    if errores:
        print("Últimos errores:")
        for id_, tipo, msg, creado in errores:
            cuando = creado.strftime("%d/%m %H:%M") if creado else "—"
            print(f"  #{id_} [{tipo}] {cuando} — {msg}")
    return 0


def modo_once(cfg: dict) -> int:
    """Procesa UN trabajo pendiente y sale (debug on-site). Usa BD + impresora."""
    conn = psycopg2.connect(cfg["DATABASE_URL"])
    try:
        trabajo = reclamar_trabajo(conn, int(cfg["RESTAURANTE_ID"]))
        if not trabajo:
            print("[once] no hay trabajos pendientes.")
            return 0
        return 0 if _imprimir_trabajo(conn, trabajo, cfg["PRINTER_CONNECTION"]) else 1
    finally:
        conn.close()


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
    ticket. Incrementa 'intentos' (1 en el primer claim, 2+ en cada reintento) y lo
    devuelve para que _imprimir_trabajo no reabra el cajón en reimpresiones. Devuelve
    {id, tipo, payload, intentos} o None.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE print_jobs
               SET estado = 'imprimiendo', intentos = intentos + 1, reclamado_at = NOW()
            WHERE id = (
                SELECT id FROM print_jobs
                WHERE restaurante_id = %s AND estado = 'pendiente'
                ORDER BY creado_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, tipo, payload, intentos
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


def _imprimir_trabajo(conn, trabajo, printer_cfg) -> bool:
    """Imprime un trabajo YA reclamado (recibo o comanda) y cierra su estado en la BD.
    Errores de impresora → marca 'error' y devuelve False. Errores de BD al marcar
    estado SUBEN al caller (para reconectar)."""
    job_id = trabajo["id"]
    tipo = (trabajo.get("tipo") or "recibo").lower()
    payload = trabajo["payload"]
    if isinstance(payload, str):  # por si el driver no deserializa el JSONB
        payload = json.loads(payload)

    # Anti doble-cajón: el pulso de apertura SOLO se manda en el PRIMER intento. Si un
    # recibo falló a mitad de impresión DESPUÉS de abrir el cajón (sin papel, buffer, USB
    # suelto) y el janitor lo re-encola, al reimprimirlo NO debe reabrir el cajón —eso
    # descuadraría el arqueo y es una superficie de robo—. reclamar_trabajo deja intentos=1
    # en el primer claim y 2+ en cada reintento. El recibo (texto) sí se reimprime entero;
    # lo único que se suprime es el pulso del cajón.
    intentos = int(trabajo.get("intentos") or 1)
    if intentos > 1 and isinstance(payload, dict) and payload.get("abrir_cajon"):
        payload = dict(payload, abrir_cajon=False)
        print(f"[agent] job #{job_id} es reintento #{intentos}: el cajón NO se reabre")

    print(f"[agent] imprimiendo job #{job_id} ({tipo}, cajon={payload.get('abrir_cajon')})")
    try:
        printer = abrir_impresora(printer_cfg)
        if tipo == "comanda":
            imprimir_comanda(printer, payload)
        elif tipo == "prerecibo":
            imprimir_prerecibo(printer, payload)
        else:
            imprimir_recibo(printer, payload)
        try:
            printer.close()
        except Exception:
            pass
    except Exception as exc:
        # Impresora desconectada / sin papel / etc. → flag 'error' + log.
        print(f"[agent] job #{job_id} FALLÓ: {exc}")
        marcar_error(conn, job_id, str(exc))
        return False
    marcar_impreso(conn, job_id)
    print(f"[agent] job #{job_id} OK")
    return True


# ── Mantenimiento de cola (janitor) + latido (heartbeat) ─────────────────────────
def _ensure_agent_schema(conn) -> None:
    """Garantiza las piezas que el agente necesita aunque arranque antes que el panel:
    la columna reclamado_at (marca cuándo se tomó un job, para detectar atascos) y la
    tabla agentes_estado (latido). El agente es autosuficiente: no depende del panel."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE print_jobs ADD COLUMN IF NOT EXISTS reclamado_at TIMESTAMP")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agentes_estado (
                restaurante_id INTEGER   PRIMARY KEY,
                hostname       VARCHAR(120),
                visto_at       TIMESTAMP NOT NULL DEFAULT NOW(),
                cola_pendiente INTEGER   NOT NULL DEFAULT 0
            )
        """)
    conn.commit()


def recuperar_huerfanos(conn, restaurante_id: int, max_intentos: int = 5) -> None:
    """Janitor de cada ciclo. Dos limpiezas:
    1) Jobs atascados en 'imprimiendo' > 2 min (el agente murió tras reclamar pero antes
       de cerrar el estado) → vuelven a 'pendiente'. Se mide por reclamado_at (cuándo se
       tomó), NO por creado_at, para no re-encolar un ticket que se está imprimiendo ahora.
    2) Jobs en 'error' con intentos < max → vuelven a 'pendiente' (reintento de fallos
       transitorios: sin papel, tapa abierta, USB suelto). Pasado el tope quedan 'error'
       terminal para no reimprimir en bucle un payload imposible.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE print_jobs SET estado = 'pendiente'
            WHERE restaurante_id = %s AND estado = 'imprimiendo'
              AND reclamado_at IS NOT NULL
              AND reclamado_at < NOW() - INTERVAL '2 minutes'
        """, (restaurante_id,))
        huerfanos = cur.rowcount or 0
        cur.execute("""
            UPDATE print_jobs SET estado = 'pendiente'
            WHERE restaurante_id = %s AND estado = 'error' AND intentos < %s
        """, (restaurante_id, max_intentos))
        reintentos = cur.rowcount or 0
    conn.commit()
    if huerfanos or reintentos:
        print(f"[agent] janitor · {huerfanos} re-encolado(s) por atasco, "
              f"{reintentos} reintento(s) de error")


def heartbeat(conn, restaurante_id: int) -> None:
    """Upsert del latido en agentes_estado: visto_at=NOW() + profundidad de cola pendiente.
    El panel lo lee para pintar el badge '🟢 en línea / 🔴 sin conexión · N en cola'."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM print_jobs "
                    "WHERE restaurante_id = %s AND estado = 'pendiente'", (restaurante_id,))
        cola = int(cur.fetchone()[0])
        cur.execute("""
            INSERT INTO agentes_estado (restaurante_id, hostname, visto_at, cola_pendiente)
            VALUES (%s, %s, NOW(), %s)
            ON CONFLICT (restaurante_id) DO UPDATE
              SET visto_at = NOW(), cola_pendiente = EXCLUDED.cola_pendiente,
                  hostname = EXCLUDED.hostname
        """, (restaurante_id, socket.gethostname()[:120], cola))
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
    parser.add_argument("--status", action="store_true",
                        help="Muestra el conteo de la cola por estado y los últimos errores, y sale.")
    parser.add_argument("--once", action="store_true",
                        help="Procesa un único trabajo pendiente y sale (debug on-site).")
    args = parser.parse_args()

    if args.list_printers:
        sys.exit(modo_list_printers())
    if args.dry_run:
        sys.exit(modo_dry_run())
    if args.status:
        # Solo lee la cola: necesita BD + tenant, no la impresora.
        sys.exit(modo_status(cargar_config(requeridas=("DATABASE_URL", "RESTAURANTE_ID"))))
    if args.test:
        # Solo necesitamos la sección de impresora para validar el hardware.
        sys.exit(modo_test(cargar_config(requeridas=("PRINTER_CONNECTION",))))
    if args.once:
        sys.exit(modo_once(cargar_config()))

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
                _ensure_agent_schema(conn)
            # Cada ciclo: limpia atascos / reintenta errores transitorios y emite el latido
            # (barato e indexado) ANTES de reclamar el siguiente trabajo.
            recuperar_huerfanos(conn, restaurante_id)
            heartbeat(conn, restaurante_id)
            trabajo = reclamar_trabajo(conn, restaurante_id)
        except Exception as exc:  # caída de BD → reintentar tras el sleep
            print(f"[agent] error de BD: {exc}")
            conn = None
            time.sleep(poll)
            continue

        if not trabajo:
            time.sleep(poll)
            continue

        try:
            _imprimir_trabajo(conn, trabajo, printer_cfg)
        except Exception as exc:  # fallo de BD al cerrar el estado → reconectar
            print(f"[agent] error de BD al cerrar job: {exc}")
            conn = None

    if conn and not conn.closed:
        conn.close()
    print("[agent] detenido.")


if __name__ == "__main__":
    main()
