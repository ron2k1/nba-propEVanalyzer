param(
    [string]$RunValueName = "NBADataAutoCheck",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

$StartScript = (Resolve-Path (Join-Path $PSScriptRoot "start_autocheck.ps1")).Path
$PowerShellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path $PowerShellExe)) {
    $PowerShellExe = "powershell.exe"
}

$TaskAction = "$PowerShellExe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StartScript`""
$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

try {
    New-ItemProperty -Path $regPath -Name $RunValueName -Value $TaskAction -PropertyType String -Force | Out-Null
} catch {
    throw "Failed to set startup Run key for auto-check. $($_.Exception.Message)"
}

if ($RunNow) {
    Start-Process -FilePath $PowerShellExe -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", $StartScript -WindowStyle Hidden
}

Write-Output "Auto-check startup registration complete."
Write-Output "Mode: registry_run_key"
Write-Output "Command: $TaskAction"
