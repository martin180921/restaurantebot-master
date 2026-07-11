# Runbook de soporte

Qué hacer cuando un restaurante llama con un problema. Cada sección va de la causa más común a la más rara — sigue la lista en orden, no saltes. Las señales 🔴/🟡 son las que reporta `monitor_salud.py` ([MONITOREO.md](MONITOREO.md)).

**Primer paso siempre:** correr el monitor contra ese restaurante para saber qué ve el sistema antes de creer el diagnóstico del teléfono:

```bash
python scripts/monitor_salud.py --database-url "postgresql://...ese-restaurante..." --nombre "Doña Marta"
```

---

## 🖨️ "No imprime" (la llamada más frecuente)

Señales del monitor: *Agente sin latir*, *Comandas en `error`*, *Cola `pendiente` atascada*.

1. **¿La impresora tiene papel y está encendida?** Pídeles verificar antes que nada. Si el cabezal está abierto o sin papel, los trabajos quedan `pendiente`/`error`.
2. **¿El PC del local está encendido y con internet?** El agente corre ahí. Si reiniciaron el PC, la tarea programada `PrintAgent` arranca sola al iniciar sesión — pídeles iniciar sesión en Windows.
3. **Reiniciar el agente** (en el PC del local): doble clic en `print_agent/reiniciar_agente.bat`. Verifica que la tarea quede *Running*.
4. **Ver el log**: `print_agent/agent.log` (el agente corre sin ventana; ahí van sus errores). Errores típicos: nombre de impresora cambiado en Windows (corregir `printer_name` en `config.json`, listar con `python agent.py --list-printers`), o base inaccesible (ver sección Railway).
5. **Ver la cola desde tu PC**: `python agent.py --status` (solo lee la BD) muestra pendientes y últimos errores.
6. **Reencolar un trabajo fallido** una vez resuelta la causa:
   ```sql
   UPDATE print_jobs SET estado='pendiente' WHERE id=...;
   ```
7. Prueba final con el restaurante: cobrar un pedido de prueba o `python agent.py --test` (imprime recibo de muestra real y abre el cajón, sin tocar la BD).

## 💬 "No llegan pedidos de WhatsApp"

1. **¿El servicio `whatsapp_bot` está arriba en Railway?** (proyecto del restaurante → deploy → logs). Si crasheó, Railway reintenta 3 veces y se detiene: hacer *Restart*.
2. **¿Twilio está operativo?** Revisar [status.twilio.com](https://status.twilio.com) y la consola de Twilio → Monitor → Logs → Errors (webhook fallando aparece ahí).
3. **¿El webhook apunta al servicio correcto?** En Twilio, el número debe tener como webhook la URL pública del `whatsapp_bot` de ESE restaurante (`/webhook`).
4. **¿Crédito/estado de la cuenta Twilio?** Cuenta suspendida o sin saldo = silencio total sin error visible en Railway.

## 🖥️ "El panel no abre" / "la carta digital no carga"

1. **Railway**: revisar el deploy del servicio (`dashboard_admin` o `app_cliente`) en el proyecto de ese restaurante; *Restart* si está caído. Estado general: [status.railway.com](https://status.railway.com).
2. **¿Es solo login?** Contraseña admin/caja mal escrita bloquea temporalmente por intentos (tabla `login_intentos`). Esperar unos minutos o verificar la contraseña de entorno.
3. **PIN de mesero no funciona**: los PIN son de turno (efímeros). Generar uno nuevo desde Caja con el rol caja/admin.

## 🔴 "No conecta a la base" (monitor en ALERTA)

1. ¿Railway caído? → status page. ¿Solo esa base? → revisar el servicio PostgreSQL del proyecto.
2. ¿Cambiaron credenciales/URL? La `DATABASE_URL` debe coincidir en: los 3 servicios de Railway **y** el `config.json` del print_agent del local.
3. Si la base se perdió de verdad: restaurar el último respaldo — procedimiento en [BACKUPS.md](BACKUPS.md) (`restore_db.py` es destructivo: confirma dos veces contra qué base corres).

## 📋 Datos raros (menú duplicado, quieren "empezar de cero")

- Platos duplicados en el menú: `scripts/sql/limpiar_menu_duplicados.sql`.
- Reiniciar un restaurante de **prueba**: `scripts/sql/reset_datos.sql` — ⚠️ destructivo, jamás contra producción sin respaldo del mismo día.

## Después de cada incidente

1. Anotarlo en la página del restaurante en Notion (🏪 Restaurantes) — qué pasó, causa, cuánto tardó.
2. Si expuso un bug o algo mejorable, crear la tarea en 💻 Tareas de Código con Tipo=Bug.
3. Si se repite, es señal de automatizar la detección (agregarla a `monitor_salud.py`) o la solución.
