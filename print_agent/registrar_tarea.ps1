# Registra (o reemplaza) la tarea programada 'PrintAgent' con ajustes robustos:
# sin limite de 72 h, corre y sigue con bateria, se reinicia sola si cae.
# Requiere administrador (lo garantizan instalar_agente.bat y endurecer_tarea.bat).
$ErrorActionPreference = 'Stop'

$py  = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$ag  = Join-Path $PSScriptRoot 'agent.py'
if (-not (Test-Path $py)) { throw "No existe $py. Corre instalar_agente.bat primero." }

$accion     = New-ScheduledTaskAction -Execute $py -Argument "`"$ag`"" -WorkingDirectory $PSScriptRoot
$disparador = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$ajustes    = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
              -StartWhenAvailable -MultipleInstances IgnoreNew
$principal  = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName 'PrintAgent' -Action $accion -Trigger $disparador `
    -Settings $ajustes -Principal $principal -Force | Out-Null

# El PC del local no debe dormirse ni suspender USB.
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4fe5-a3a0-cd13ec28fb6e 0
powercfg /setactive SCHEME_CURRENT

Start-ScheduledTask -TaskName 'PrintAgent'
Start-Sleep -Seconds 3
$t = Get-ScheduledTask -TaskName 'PrintAgent'
$t.Settings | Select-Object ExecutionTimeLimit, RestartCount, DisallowStartIfOnBatteries, StopIfGoingOnBatteries
if ($t.State -ne 'Running') { throw "La tarea NO quedo corriendo. Estado: $($t.State)" }
Write-Host 'PrintAgent registrado y corriendo.'
