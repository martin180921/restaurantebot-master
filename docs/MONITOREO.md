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
3. Crea `scripts/monitor_salud.bat` (junto al script; no se sube a git) con esto — el
   Programador de tareas de Windows no tiene forma directa de fijar variables de entorno solo
   para una acción, así que un `.bat` envoltorio es la manera práctica de dejarlas puestas:
   ```bat
   @echo off
   set TELEGRAM_BOT_TOKEN=123456:ABC...
   set TELEGRAM_CHAT_ID=987654321
   "C:\ruta\completa\a\python.exe" "%~dp0monitor_salud.py" --config "%~dp0restaurantes_monitor.json"
   ```
   Pruébalo a mano primero (doble clic) sin problemas configurados: debe imprimir en consola
   sin avisar por Telegram. Si hay algún problema, te llega el resumen por Telegram; sin esas
   variables, solo imprime en consola.

## Dejarlo corriendo cada pocos minutos

Programador de tareas de Windows → *Crear tarea básica* → desencadenador **Repetir cada 5
minutos** → acción **Iniciar un programa** apuntando directo al
`scripts/monitor_salud.bat` de arriba (así las variables de Telegram viajan con él, sin
depender del entorno de la tarea). Complementa esto con un **UptimeRobot** gratuito sobre las
URLs de los paneles para cubrir el caso de que el propio PC del monitor se apague.
