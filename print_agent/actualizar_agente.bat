@echo off
REM Actualiza el Agente de Impresion: baja el codigo mas reciente (git pull) y
REM reinicia la tarea programada 'PrintAgent' para que cargue el nuevo agent.py.
REM Doble clic en este archivo en el PC del restaurante despues de cada cambio.
setlocal
cd /d "%~dp0"

echo ================================
echo  Actualizando Agente de Impresion
echo ================================
echo.

git pull
if errorlevel 1 (
    echo.
    echo [ERROR] git pull fallo. Revisa la conexion a internet o avisa a soporte.
    echo El agente sigue corriendo con la version anterior, sin cambios.
    pause
    exit /b 1
)

echo.
echo Reiniciando el Agente de Impresion...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Stop-ScheduledTask -TaskName 'PrintAgent' -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; Start-ScheduledTask -TaskName 'PrintAgent'"
if errorlevel 1 (
    echo.
    echo [ERROR] No se pudo reiniciar la tarea 'PrintAgent'. Avisa a soporte.
    pause
    exit /b 1
)

echo.
echo ================================
echo  Listo! Agente actualizado y corriendo.
echo ================================
pause
