@echo off
REM Instala el Agente de Impresion en este PC: crea el entorno virtual, instala las
REM dependencias, corre el asistente de configuracion (BD, restaurante, impresora) y
REM registra la tarea programada 'PrintAgent' para que arranque solo al iniciar sesion.
REM Doble clic en este archivo en el PC del restaurante (una sola vez por local).
REM Se puede correr de nuevo sin miedo: reinstala/reconfigura sin duplicar nada.
setlocal
cd /d "%~dp0"

REM --- Auto-elevacion a administrador (crear la tarea programada la requiere) ----
REM fltmc (y no 'net session') como chequeo: funciona aunque el servicio 'Servidor'
REM este deshabilitado. El marcador ELEVADO evita relanzarse en bucle si aun asi
REM no se obtienen permisos (p. ej. el usuario cancela el UAC).
fltmc >nul 2>&1
if errorlevel 1 (
    if "%~1"=="ELEVADO" (
        echo [ERROR] No se pudieron obtener permisos de administrador.
        echo Cierra esta ventana y corre el instalador con clic derecho ^> "Ejecutar como administrador".
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
echo  Instalando Agente de Impresion
echo ================================
echo.

REM --- Python -------------------------------------------------------------------
set "PYCMD="
py -3 --version >nul 2>&1
if errorlevel 1 (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo.
        echo [ERROR] No se encontro Python instalado.
        echo Instala Python 3 desde https://www.python.org/downloads/ ^(marca "Add to PATH"^)
        echo y vuelve a correr este instalador.
        pause
        exit /b 1
    )
    set "PYCMD=python"
) else (
    set "PYCMD=py -3"
)
echo Python detectado: %PYCMD%

REM --- Entorno virtual ------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Creando entorno virtual...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo.
        echo [ERROR] No se pudo crear el entorno virtual. Avisa a soporte.
        pause
        exit /b 1
    )
)

echo.
echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] No se pudieron instalar las dependencias. Revisa la conexion a internet.
    pause
    exit /b 1
)

REM --- Configuracion (BD, restaurante, impresora) ---------------------------------
echo.
echo ================================
echo  Configuracion
echo ================================
".venv\Scripts\python.exe" agent.py --setup
if errorlevel 1 (
    echo.
    echo [ERROR] La configuracion no se completo o se cancelo. Corre otra vez este
    echo instalador cuando tengas a mano la cadena de conexion, el ID del restaurante
    echo y la impresora.
    pause
    exit /b 1
)

REM --- Tarea programada (arranca solo al iniciar sesion, sin ventana visible) -----
REM Si es una REINSTALACION y el agente viejo sigue corriendo, hay que pararlo antes:
REM /Create /F sobrescribe la definicion de la tarea pero NO mata el proceso anterior,
REM que seguiria imprimiendo con la config vieja. /End es inofensivo si no existe.
schtasks /End /TN "PrintAgent" >nul 2>&1

echo.
echo Registrando la tarea programada 'PrintAgent'...
schtasks /Create /F /TN "PrintAgent" /SC ONLOGON ^
    /TR "\"%~dp0.venv\Scripts\pythonw.exe\" \"%~dp0agent.py\"" >nul
if errorlevel 1 (
    echo.
    echo [ERROR] No se pudo crear la tarea programada. Avisa a soporte.
    pause
    exit /b 1
)

REM try/catch + exit 1: sin esto, powershell.exe devuelve 0 aunque Start-ScheduledTask
REM falle (error no-terminante) y el 'if errorlevel 1' nunca detectaria el fallo.
REM Ademas verifica que la tarea quedo Running, no solo que el Start no exploto.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { Start-ScheduledTask -TaskName 'PrintAgent' -ErrorAction Stop; Start-Sleep -Seconds 2; $t = Get-ScheduledTask -TaskName 'PrintAgent' -ErrorAction Stop; if ($t.State -ne 'Running') { Write-Host ('La tarea NO quedo corriendo. Estado: ' + $t.State); exit 1 } } catch { Write-Host ('Fallo: ' + $_); exit 1 }"
if errorlevel 1 (
    echo.
    echo [ERROR] La tarea se creo pero no quedo corriendo. Avisa a soporte.
    pause
    exit /b 1
)

echo.
echo ================================
echo  Listo! Agente instalado y corriendo.
echo ================================
echo.
echo - Corre en segundo plano ^(sin ventana^) y arranca solo al iniciar sesion
echo   de ESTE usuario de Windows. Si el PC usa otra cuenta a diario, corre el
echo   instalador desde esa cuenta.
echo - Log: %~dp0agent.log
echo - Para reiniciarlo sin bajar cambios: reiniciar_agente.bat
echo - Para actualizarlo tras un cambio en el codigo: actualizar_agente.bat
pause
