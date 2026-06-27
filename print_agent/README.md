# Print Agent (Agente de Impresión Local)

Corre **dentro del restaurante**, en el PC conectado a la Epson 80mm + cajón SAT.
Hace polling a la cola `print_jobs` de la BD en la nube (Railway) y imprime los
tickets de su `RESTAURANTE_ID`. Es independiente del panel Streamlit: solo comparte
la tabla `print_jobs` como contrato.

## Instalación (una vez por local)

```bash
cd print_agent
python -m venv .venv && .venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy config.example.json config.json                  # y edítalo
python agent.py
```

Para que arranque solo al encender el PC: Task Scheduler (Windows) o un servicio
systemd (Linux/Raspberry Pi) ejecutando `python agent.py`.

## Probar antes de producción

```bash
python agent.py --dry-run   # imprime un recibo de muestra como TEXTO en consola
                            # (no usa impresora ni BD ni escpos) → revisa el layout
python agent.py --test      # imprime el recibo de muestra en la impresora REAL
                            # y dispara el cajón (no toca la BD) → valida el hardware
python agent.py --list-printers  # (Windows) lista las impresoras para hallar printer_name
python agent.py --status    # conteo de la cola por estado + últimos errores (solo lee BD)
python agent.py --once      # procesa UN trabajo pendiente y sale (debug on-site)
```

Empieza con `--dry-run` para ver el formato; luego `--test` con la impresora
conectada para confirmar que imprime y que el cajón abre. Recién entonces `python agent.py`.

## config.json

| Clave | Qué es |
|---|---|
| `DATABASE_URL` | Cadena Postgres de Railway (la misma BD que usa el panel). |
| `RESTAURANTE_ID` | Entero único del local. Debe coincidir con el `RESTAURANTE_ID` del panel. |
| `POLL_SECONDS` | Intervalo de sondeo (default 2). |
| `PRINTER_CONNECTION` | Cómo se conecta la impresora. Tres modos abajo. |

### Modos de impresora (`PRINTER_CONNECTION.type`)

**`windows` — recomendado en PC Windows.** Imprime por el spooler usando el driver
Epson ya instalado. No necesita libusb ni Zadig.
```json
{ "type": "windows", "printer_name": "EPSON TM-T20III Receipt" }
```
El `printer_name` debe coincidir EXACTO con el nombre en Windows. Lístalo con el
propio agente:
```powershell
python agent.py --list-printers
```
(o `Get-Printer | Select-Object Name` en PowerShell).
Si omites `printer_name`, usa la impresora predeterminada. Requiere `pywin32` (se
instala solo en Windows vía requirements.txt).

**`usb` — directo por libusb** (Linux/Raspberry Pi, o Windows con driver WinUSB/Zadig).
```json
{ "type": "usb", "vendor_id": "0x04b8", "product_id": "0x0202" }
```
Halla los IDs con `lsusb` (Linux) o el Administrador de dispositivos (Windows).
`0x04b8` es Epson; el product id varía por modelo (TM-T20 ≈ `0x0202`). En Windows
este modo exige cambiar el driver a WinUSB con Zadig (la impresora deja de ser una
"impresora" normal de Windows) — por eso preferimos `windows`.

**`network` — Ethernet/Wi-Fi.**
```json
{ "type": "network", "host": "192.168.1.50", "port": 9100 }
```

## Tipos de ticket (`print_jobs.tipo`)

- **`recibo`** — se encola al **cobrar** (pago completo o parcial). Lleva ítems,
  totales, cambio y abre el cajón **solo si fue en efectivo**.
- **`comanda`** — ticket de **cocina**, se encola cuando un pedido pasa a
  *"en preparación"* (botón ▶ Iniciar preparación). Solo mesa/cliente + ítems en
  grande, **sin precios ni cajón**.
- **`prerecibo`** — **pre-cuenta** para el cliente antes de pagar (botón **🖨 Ticket**
  del Monitor). Mismo layout que el `recibo` (ítems + total en **Fuente B**) pero con
  encabezado **PRERECIBO** ("NO ES FACTURA VÁLIDA") y la **Mesa** bien visible.
  **Sin desglose de pago ni cajón.**

> El **cuerpo** del `recibo` y del `prerecibo` se imprime en **Fuente B** (más pequeña,
> ~64 col) para gastar menos papel; tras el corte se vuelve a **Fuente A** (~48 col),
> así las `comanda`s y reportes siguientes salen en el tamaño legible estándar.

## Estados de un trabajo

`pendiente` → (agente lo toma) `imprimiendo` → `impreso` · o `error` (con `error_msg`).
El claim usa `FOR UPDATE SKIP LOCKED`, así que puedes correr más de un agente sin
imprimir duplicados. Para reintentar un fallo: `UPDATE print_jobs SET estado='pendiente' WHERE id=…`.

## Cajón monedero (SAT)

El cajón se abre **solo en pagos en efectivo**: el panel pone `abrir_cajon:true` en el
payload y el agente envía el pulso `\x1b\x70\x00\x19\x96` al inicio del buffer. En
transferencia no se abre.

# Re start para actualizaciones 
Stop-ScheduledTask  -TaskName PrintAgent
Start-ScheduledTask -TaskName PrintAgent
Get-ScheduledTask   -TaskName PrintAgent
