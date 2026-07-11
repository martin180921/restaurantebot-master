@echo off
setlocal
chcp 65001 >nul
rem ============================================================================
rem  OLO - Instalador de tareas programadas de la FLOTA (1 clic)
rem  Correr en TU PC (el que administra los restaurantes), NO en el del local.
rem  Crea dos tareas de Windows:
rem    OLO_Respaldo : respaldo diario de TODAS las bases (por defecto 09:00)
rem    OLO_Monitor  : monitor de salud de la flota cada 10 minutos
rem  Ambas leen scripts\restaurantes_monitor.json (la lista de restaurantes).
rem  Re-correrlo es seguro: recrea las tareas sin tocar los wrappers ya editados.
rem ============================================================================

set "SCRIPTS=%~dp0"
set "HORA_RESPALDO=09:00"
set "MONITOR_CADA_MIN=10"

if not exist "%SCRIPTS%restaurantes_monitor.json" (
    echo [FALTA] scripts\restaurantes_monitor.json
    echo Copia restaurantes_monitor.example.json a restaurantes_monitor.json
    echo y pon ahi la lista de tus restaurantes. Luego vuelve a correr esto.
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo [FALTA] Python no esta en el PATH de este PC.
    pause
    exit /b 1
)

rem ── Wrapper del respaldo (solo si no existe: no pisa ajustes locales) ──────
if not exist "%SCRIPTS%respaldo_diario.bat" (
    >  "%SCRIPTS%respaldo_diario.bat" echo @echo off
    >> "%SCRIPTS%respaldo_diario.bat" echo cd /d %%~dp0..
    >> "%SCRIPTS%respaldo_diario.bat" echo python scripts\backup_db.py --config scripts\restaurantes_monitor.json --out-dir backups ^>^> backups\respaldo.log 2^>^&1
    echo [OK] Creado scripts\respaldo_diario.bat
)

rem ── Wrapper del monitor (solo si no existe). Edita este archivo y pon tu   ──
rem ── TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID para recibir alertas al celular. ──
if not exist "%SCRIPTS%monitor_flota.bat" (
    >  "%SCRIPTS%monitor_flota.bat" echo @echo off
    >> "%SCRIPTS%monitor_flota.bat" echo rem Rellena estas dos lineas para recibir alertas por Telegram (opcional):
    >> "%SCRIPTS%monitor_flota.bat" echo set TELEGRAM_BOT_TOKEN=
    >> "%SCRIPTS%monitor_flota.bat" echo set TELEGRAM_CHAT_ID=
    >> "%SCRIPTS%monitor_flota.bat" echo cd /d %%~dp0..
    >> "%SCRIPTS%monitor_flota.bat" echo python scripts\monitor_salud.py --config scripts\restaurantes_monitor.json ^>^> scripts\monitor.log 2^>^&1
    echo [OK] Creado scripts\monitor_flota.bat
)

rem ── Tareas programadas (recrear con /F es idempotente) ─────────────────────
schtasks /Create /F /TN "OLO_Respaldo" /SC DAILY /ST %HORA_RESPALDO% /TR "\"%SCRIPTS%respaldo_diario.bat\"" >nul
if errorlevel 1 (
    echo [ERROR] No se pudo crear la tarea OLO_Respaldo.
    pause
    exit /b 1
)
echo [OK] Tarea OLO_Respaldo: respaldo diario de la flota a las %HORA_RESPALDO%.

schtasks /Create /F /TN "OLO_Monitor" /SC MINUTE /MO %MONITOR_CADA_MIN% /TR "\"%SCRIPTS%monitor_flota.bat\"" >nul
if errorlevel 1 (
    echo [ERROR] No se pudo crear la tarea OLO_Monitor.
    pause
    exit /b 1
)
echo [OK] Tarea OLO_Monitor: monitor de salud cada %MONITOR_CADA_MIN% minutos.

echo.
echo Listo. Verificacion rapida:
echo   - Corre ahora un respaldo de prueba:   schtasks /Run /TN OLO_Respaldo
echo   - Revisa backups\respaldo.log y scripts\monitor.log
echo   - Para alertas de Telegram edita scripts\monitor_flota.bat (token y chat id)
echo   - El PC debe estar encendido a las %HORA_RESPALDO% para el respaldo diario.
pause
