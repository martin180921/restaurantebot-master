@echo off
REM Reinicia el Agente de Impresion sin bajar cambios de git (solo Stop/Start).
REM Util cuando el agente se cuelga o hay que aplicar un config.json editado a mano.
setlocal
cd /d "%~dp0"

REM --- Auto-elevacion a administrador (Start/Stop-ScheduledTask puede exigirla mas ----
REM abajo: la tarea la crea instalar_agente.bat ya elevado, y Windows a veces deja el
REM permiso de arrancarla/pararla restringido a admin aunque corra como usuario normal
REM - un doble clic sin esto puede parar el agente viejo y no poder levantarlo de nuevo).
REM Mismo truco que instalar_agente.bat: fltmc como chequeo (funciona aunque el
REM servicio 'Servidor' este deshabilitado), marcador ELEVADO anti-bucle.
fltmc >nul 2>&1
if errorlevel 1 (
    if "%~1"=="ELEVADO" (
        echo [ERROR] No se pudieron obtener permisos de administrador.
        echo Cierra esta ventana y corre este archivo con clic derecho ^> "Ejecutar como administrador".
        pause
        exit /b 1
    )
    echo Se necesitan permisos de administrador. Reabriendo con elevacion...
    echo ^(Si no aparece la ventana nueva, acepta el aviso de Windows o corre este
    echo archivo con clic derecho ^> "Ejecutar como administrador".^)
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList 'ELEVADO' -Verb RunAs"
    exit /b
)

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
