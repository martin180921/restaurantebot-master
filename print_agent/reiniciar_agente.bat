@echo off
REM Reinicia el Agente de Impresion sin bajar cambios de git (solo Stop/Start).
REM Util cuando el agente se cuelga o hay que aplicar un config.json editado a mano.
setlocal
cd /d "%~dp0"

echo ================================
echo  Reiniciando Agente de Impresion
echo ================================
echo.

REM try/catch + exit 1: sin esto, powershell.exe devuelve 0 aunque Start-ScheduledTask
REM falle (error no-terminante) y el 'if errorlevel 1' nunca detectaria el fallo.
REM Ademas verifica que la tarea quedo Running, no solo que el Start no exploto.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { Stop-ScheduledTask -TaskName 'PrintAgent' -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; Start-ScheduledTask -TaskName 'PrintAgent' -ErrorAction Stop; Start-Sleep -Seconds 2; $t = Get-ScheduledTask -TaskName 'PrintAgent' -ErrorAction Stop; $t; if ($t.State -ne 'Running') { Write-Host ('La tarea NO quedo corriendo. Estado: ' + $t.State); exit 1 } } catch { Write-Host ('Fallo: ' + $_); exit 1 }"
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
