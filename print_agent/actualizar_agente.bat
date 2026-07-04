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

REM Instala dependencias nuevas ANTES de reiniciar: si un cambio agrego un paquete
REM a requirements.txt y no se instala, el agente reiniciaria y moriria al importar.
if exist ".venv\Scripts\python.exe" (
    echo.
    echo Actualizando dependencias...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo.
        echo [ERROR] No se pudieron instalar las dependencias. Avisa a soporte.
        echo El agente sigue corriendo con la version anterior, sin reiniciar.
        pause
        exit /b 1
    )
)

echo.
echo Reiniciando el Agente de Impresion...
REM try/catch + exit 1: sin esto, powershell.exe devuelve 0 aunque Start-ScheduledTask
REM falle (error no-terminante) y el 'if errorlevel 1' nunca detectaria el fallo.
REM Ademas verifica que la tarea quedo Running, no solo que el Start no exploto.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { Stop-ScheduledTask -TaskName 'PrintAgent' -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; Start-ScheduledTask -TaskName 'PrintAgent' -ErrorAction Stop; Start-Sleep -Seconds 2; $t = Get-ScheduledTask -TaskName 'PrintAgent' -ErrorAction Stop; if ($t.State -ne 'Running') { Write-Host ('La tarea NO quedo corriendo. Estado: ' + $t.State); exit 1 } } catch { Write-Host ('Fallo: ' + $_); exit 1 }"
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
