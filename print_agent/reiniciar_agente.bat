@echo off
REM Reinicia el Agente de Impresion sin bajar cambios de git (solo Stop/Start).
REM Util cuando el agente se cuelga o hay que aplicar un config.json editado a mano.
setlocal
cd /d "%~dp0"

echo ================================
echo  Reiniciando Agente de Impresion
echo ================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Stop-ScheduledTask -TaskName 'PrintAgent' -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; Start-ScheduledTask -TaskName 'PrintAgent'; Start-Sleep -Seconds 1; Get-ScheduledTask -TaskName 'PrintAgent'"
if errorlevel 1 (
    echo.
    echo [ERROR] No se pudo reiniciar la tarea 'PrintAgent'. Avisa a soporte.
    pause
    exit /b 1
)

echo.
echo ================================
echo  Listo! Agente reiniciado.
echo ================================
pause
