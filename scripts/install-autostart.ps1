param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskName = 'JobPilot Autostart'
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'JobPilot Dashboard.lnk') -ErrorAction SilentlyContinue
    Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) 'JobPilot Dashboard.lnk') -ErrorAction SilentlyContinue
    Write-Host 'JobPilot sign-in task and shortcuts removed.'
    exit
}
$pythonwCandidates = @(
    (Join-Path $root '.venv\Scripts\pythonw.exe'),
    (Join-Path $root '.venv-local-ai\Scripts\pythonw.exe')
)
$pythonw = $null
foreach ($candidate in $pythonwCandidates) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    $candidatePython = Join-Path (Split-Path -Parent $candidate) 'python.exe'
    if (Test-Path -LiteralPath $candidatePython) {
        & $candidatePython -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(n) for n in ('fastapi','uvicorn','playwright')) else 1)" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $pythonw = $candidate
            break
        }
    }
}
if (-not $pythonw) {
    try {
        $pythonExe = (& python -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1).Trim()
        $dependenciesAvailable = $false
        if ($pythonExe) {
            & $pythonExe -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(n) for n in ('fastapi','uvicorn','playwright')) else 1)" 2>$null | Out-Null
            $dependenciesAvailable = $LASTEXITCODE -eq 0
        }
        if ($pythonExe -and $dependenciesAvailable) {
            $siblingPythonw = Join-Path (Split-Path -Parent $pythonExe) 'pythonw.exe'
            if (Test-Path -LiteralPath $siblingPythonw) { $pythonw = $siblingPythonw }
        }
    } catch { }
}
if (-not $pythonw) {
    throw 'Could not find pythonw.exe. Install Python or create the project virtual environment, then rerun this installer.'
}
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('-X utf8 "' + (Join-Path $root 'autostart.py') + '"') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = 'PT30S'
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

# Create Startup folder shortcut so browser opens reliably on user logon
$wsh = New-Object -ComObject WScript.Shell
$startupPath = [Environment]::GetFolderPath('Startup')
if (Test-Path -LiteralPath $startupPath) {
    $startupLnk = $wsh.CreateShortcut((Join-Path $startupPath 'JobPilot Dashboard.lnk'))
    $startupLnk.TargetPath = $pythonw
    $startupLnk.Arguments = ('-X utf8 "' + (Join-Path $root 'agent_launcher.py') + '"')
    $startupLnk.WorkingDirectory = $root
    $startupLnk.IconLocation = "$pythonw,0"
    $startupLnk.Description = 'Open JobPilot Dashboard'
    $startupLnk.Save()
}

# Create Desktop shortcut for easy access
$desktopPath = [Environment]::GetFolderPath('Desktop')
if (Test-Path -LiteralPath $desktopPath) {
    $desktopLnk = $wsh.CreateShortcut((Join-Path $desktopPath 'JobPilot Dashboard.lnk'))
    $desktopLnk.TargetPath = $pythonw
    $desktopLnk.Arguments = ('-X utf8 "' + (Join-Path $root 'agent_launcher.py') + '"')
    $desktopLnk.WorkingDirectory = $root
    $desktopLnk.IconLocation = "$pythonw,0"
    $desktopLnk.Description = 'Open JobPilot Dashboard'
    $desktopLnk.Save()
}

Write-Host "JobPilot sign-in task and shortcuts registered and started for $env:USERDOMAIN\$env:USERNAME using $pythonw."
