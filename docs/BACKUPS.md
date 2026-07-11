# Respaldos de la base de datos

Cada restaurante tiene su propia base en Railway. Estos respaldos son el seguro contra un
borrado accidental, un problema de Railway o una migración que salga mal: sin ellos, un solo
percance le pierde la caja y las ventas a un cliente. **Antes de poner el primer restaurante
en producción, deja el respaldo diario andando.**

Son puro Python (solo `psycopg2`): no necesitan `pg_dump` ni `psql`, y funcionan contra
Postgres 18 de Railway desde cualquier PC con Windows.

## Respaldar (seguro, solo lectura)

```bash
python scripts/backup_db.py --database-url "postgresql://...restaurante..." \
    --nombre "Doña Marta" --out-dir backups
```

Crea `backups/backup_<nombre>_<bd>_<fecha_hora>.sql.gz` con los datos de todas las tablas, y
**poda** los respaldos con más de 14 días (conservando siempre los 7 más nuevos **por
restaurante**: la poda agrupa por `--nombre`). Ajustable con `--retention-days` y `--keep`. Es
solo lectura sobre la base (transacción `REPEATABLE READ`: todas las tablas quedan
congeladas en el mismo instante): seguro de correr en producción en cualquier momento.

Usa siempre `--nombre` si vas a guardar backups de varios restaurantes en el mismo lugar:
todos comparten el mismo nombre de base en Railway ("railway"), así que sin `--nombre` los
archivos de 5 restaurantes serían indistinguibles.

## Toda la flota de una pasada

Con el mismo JSON que usa el monitor (`scripts/restaurantes_monitor.json`, una entrada por
restaurante) se respalda todo en un comando; un restaurante caído no detiene a los demás,
pero el código de salida ≠ 0 avisa:

```bash
python scripts/backup_db.py --config scripts/restaurantes_monitor.json --out-dir backups
```

## Restaurar (destructivo — reemplaza los datos)

```bash
# 1) Ensayo: parsea el archivo y dice qué haría, sin tocar ninguna base.
python scripts/restore_db.py --file backups/backup_....sql.gz --dry-run

# 2) Real: exige --yes. Aplica db/schema.sql (crea tablas si faltan) y recarga los datos.
python scripts/restore_db.py --file backups/backup_....sql.gz \
    --database-url "postgresql://...DESTINO..." --yes
```

⚠️ Verifica DOS VECES la `--database-url` de destino: la restauración vacía y recarga cada
tabla. Nunca apuntes a la base de un restaurante en marcha salvo que restaurar sea justo lo
que quieres.

## Dejarlo automático

**Opción A — 1 clic, desde tu PC (recomendada para el piloto):** doble clic en
`scripts/instalar_flota.bat`. Crea las tareas programadas `OLO_Respaldo` (respaldo diario de
toda la flota vía `--config`, 09:00 por defecto) y `OLO_Monitor` (monitor cada 10 min), con
sus wrappers editables en `scripts/`. Tu PC debe estar encendido a esa hora; verifica el
primer día con `schtasks /Run /TN OLO_Respaldo` y mirando `backups/respaldo.log`.

**Opción B — en el PC del restaurante:** el mismo PC que corre el `print_agent` (siempre
encendido) respalda su propia base cada noche. El archivo queda en el disco del local,
físicamente separado de Railway.

Programador de tareas de Windows (una sola vez):

1. Abre **Programador de tareas** → *Crear tarea básica*.
2. Desencadenador: **Diariamente**, p. ej. 3:00 a.m. (fuera del horario de servicio).
3. Acción: **Iniciar un programa**.
   - Programa: la **ruta completa** al ejecutable, no solo `python` — la tarea corre sin el
     `PATH` de tu sesión de usuario y "python" a secas no se encuentra. Averíguala con
     `where python` en una terminal normal (algo como
     `C:\Users\<tú>\AppData\Local\Programs\Python\Python313\python.exe`).
   - Argumentos: `C:\ruta\a\scripts\backup_db.py --database-url "postgresql://..." --nombre "Doña Marta" --out-dir C:\olo\backups`
4. Marca *Ejecutar aunque el usuario no haya iniciado sesión* para que corra sin nadie logueado.

> Verifica al día siguiente que apareció el primer `.sql.gz` en la carpeta.

## Ensayo de restauración (hazlo una vez antes del piloto)

Un respaldo que nunca se probó no es un respaldo. Antes de confiar en él:

1. Crea una base Postgres vacía de prueba en Railway.
2. Restaura ahí el último backup con `--yes`.
3. Levanta el panel apuntando a esa base y confirma que ves los datos (mesas, menú, ventas).

Si eso funciona una vez, ya sabes que el proceso de recuperación sirve el día que lo necesites.
