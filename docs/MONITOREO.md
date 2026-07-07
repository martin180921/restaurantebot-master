# Monitoreo de la flota

Con varios restaurantes no puedes estar mirando 5 paneles. `scripts/monitor_salud.py` revisa
todas las bases de una pasada y avisa si algo anda mal — sobre todo lo que el cliente notaría
antes que tú: **que dejó de imprimir**.

Puro Python (solo `psycopg2` + librería estándar): corre en cualquier PC o en un cron.

## Qué detecta (por restaurante)

| Señal | Nivel | Qué significa |
|---|---|---|
| No conecta a la base | 🔴 ALERTA | Railway caído, credenciales mal, o la base borrada |
| Agente sin latir hace >N min (o nunca) | 🔴 ALERTA | El PC del local no está imprimiendo comandas |
| Comandas en estado `error` | 🔴 ALERTA | Algo no salió por la impresora |
| Cola `pendiente` atascada hace >N min | 🟡 AVISO | Agente caído o impresora sin papel |

Sale con código ≠ 0 si hay alguna ALERTA (para que una tarea programada dispare el aviso).

## Uso

```bash
# Un restaurante suelto:
python scripts/monitor_salud.py --database-url "postgresql://..." --nombre "Doña Marta"

# Todos a la vez (recomendado): copia la plantilla y llénala.
cp scripts/restaurantes_monitor.example.json scripts/restaurantes_monitor.json
python scripts/monitor_salud.py --config scripts/restaurantes_monitor.json
```

`restaurantes_monitor.json` contiene todas las URLs con contraseña, así que está en
`.gitignore` — no se sube nunca. El umbral de alerta es `--umbral-min` (10 por defecto).

## Aviso automático a tu teléfono (Telegram, gratis)

1. En Telegram habla con **@BotFather** → `/newbot` → te da un **token**.
2. Escríbele algo a tu bot, luego abre
   `https://api.telegram.org/bot<TOKEN>/getUpdates` y copia tu **chat id**.
3. Define las variables de entorno antes de correr el monitor:
   ```bash
   set TELEGRAM_BOT_TOKEN=123456:ABC...
   set TELEGRAM_CHAT_ID=987654321
   python scripts/monitor_salud.py --config scripts/restaurantes_monitor.json
   ```
   Si hay algún problema, te llega el resumen por Telegram. Sin esas variables, solo imprime
   en consola (útil para probar).

## Dejarlo corriendo cada pocos minutos

Programador de tareas de Windows → *Crear tarea básica* → desencadenador **Repetir cada 5
minutos**, acción `python C:\ruta\scripts\monitor_salud.py --config C:\ruta\scripts\restaurantes_monitor.json`,
con las dos variables `TELEGRAM_*` definidas en el entorno de la tarea. Complementa esto con un
**UptimeRobot** gratuito sobre las URLs de los paneles para cubrir el caso de que el propio PC
del monitor se apague.
