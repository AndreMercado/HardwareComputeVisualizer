# One-time setup of LibreHardwareMonitor (LHM) for CPU / chipset temperatures.
#
#   powershell -ExecutionPolicy Bypass -File setup_lhm.ps1
#
# 1. Downloads LHM v0.9.6 from its official GitHub release (checked against the release's SHA-256)
#    into %LOCALAPPDATA%\Programs\LibreHardwareMonitor, unless it is already there.
# 2. Configures it: start minimized to the tray, web server on port 8085 (the app reads
#    http://127.0.0.1:8085/data.json).
# 3. Asks for administrator rights once (UAC), then:
#    - registers a logon task that starts LHM elevated (it needs admin to read CPU sensors),
#    - adds a firewall rule that blocks port 8085 from other machines (only this PC reads it),
#    - starts LHM now. On first start LHM may ask to install its PawnIO driver: accept it,
#      it is what lets LHM read the CPU temperature.
# Undo: Unregister-ScheduledTask LibreHardwareMonitor; Remove-NetFirewallRule -DisplayName "LibreHardwareMonitor web (block network)"
param([switch]$Elevated)

$ErrorActionPreference = "Stop"
$dir = Join-Path $env:LOCALAPPDATA "Programs\LibreHardwareMonitor"
$exe = Join-Path $dir "LibreHardwareMonitor.exe"
$ruleName = "LibreHardwareMonitor web (block network)"

if ($Elevated) {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $task = @{
        TaskName    = "LibreHardwareMonitor"
        Action      = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $dir
        Trigger     = New-ScheduledTaskTrigger -AtLogOn -User $user
        Principal   = New-ScheduledTaskPrincipal -UserId $user -RunLevel Highest -LogonType Interactive
        Settings    = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
                          -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
        Description = "Sensor readings for HardwareComputeVisualizer"
    }
    Register-ScheduledTask @task -Force | Out-Null
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort 8085 `
            -Action Block -Profile Any | Out-Null
    }
    if (-not (Get-Process LibreHardwareMonitor -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName "LibreHardwareMonitor"
    }
    return
}

if (-not (Test-Path $exe)) {
    $url = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip"
    $sha = "086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("lhm-" + [Guid]::NewGuid())
    New-Item -ItemType Directory $tmp | Out-Null
    $zip = Join-Path $tmp "LibreHardwareMonitor.zip"
    Write-Host "Downloading LibreHardwareMonitor v0.9.6..."
    Invoke-WebRequest -UseBasicParsing $url -OutFile $zip
    if ((Get-FileHash $zip -Algorithm SHA256).Hash.ToLower() -ne $sha) {
        throw "Download doesn't match the release's SHA-256 - not installing."
    }
    Expand-Archive $zip -DestinationPath $dir -Force
    Remove-Item $tmp -Recurse -Force
}

$config = Join-Path $dir "LibreHardwareMonitor.config"
if (-not (Test-Path $config)) {
    @"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <appSettings>
    <add key="startMinMenuItem" value="true" />
    <add key="minTrayMenuItem" value="true" />
    <add key="minCloseMenuItem" value="true" />
    <add key="runWebServerMenuItem" value="true" />
    <add key="listenerIp" value="127.0.0.1" />
    <add key="listenerPort" value="8085" />
  </appSettings>
</configuration>
"@ | Set-Content $config -Encoding UTF8
}

Write-Host "Asking for administrator rights to start LHM at every logon..."
Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Elevated")
if (Get-ScheduledTask -TaskName "LibreHardwareMonitor" -ErrorAction SilentlyContinue) {
    Write-Host "Done: LibreHardwareMonitor starts with Windows (in the tray) and the app reads its temperatures."
} else {
    Write-Host "The logon task wasn't created (administrator rights declined?)."
}
