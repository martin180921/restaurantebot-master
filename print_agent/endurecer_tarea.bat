@echo off
REM Endurece la tarea programada PrintAgent en locales ya instalados (sin limite de 72 h,
REM con bateria, reinicio automatico) y evita que el PC se duerma. Doble clic y acepta UAC.
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

echo Endureciendo la tarea programada PrintAgent...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0registrar_tarea.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] No se pudo registrar la tarea. Avisa a soporte.
    pause
    exit /b 1
)
echo Listo.
pause
