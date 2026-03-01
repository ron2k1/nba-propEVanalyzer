param(
    [int]$IntervalSec = 20,
    [int]$DebounceSec = 8,
    [int]$CooldownSec = 25
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$DaemonScript = Join-Path $RepoRoot "scripts\autocheck_daemon.py"

if (-not (Test-Path $PythonExe)) {
    throw "Python venv not found at: $PythonExe"
}
if (-not (Test-Path $DaemonScript)) {
    throw "Daemon script not found at: $DaemonScript"
}

Set-Location $RepoRoot
& $PythonExe $DaemonScript --interval $IntervalSec --debounce $DebounceSec --cooldown $CooldownSec
exit $LASTEXITCODE
