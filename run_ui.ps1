param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8787,
    [string]$OddsApiKey = "9f99c64339964f169206e51b8a10d056"
)

$ErrorActionPreference = "Stop"

Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    throw "Virtual environment not found at .venv\\Scripts\\python.exe. Create it first."
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    if ($OddsApiKey) {
        # Defensive cleanup in case caller wrapped the key in quotes.
        $OddsApiKey = $OddsApiKey.Trim().Trim('"').Trim("'")
    }
    $args = @(
        "-ExecutionPolicy", "Bypass",
        "-File", $PSCommandPath,
        "-HostAddress", $HostAddress,
        "-Port", "$Port",
        "-OddsApiKey", "$OddsApiKey"
    )
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $args | Out-Null
    Write-Host "Relaunching as Administrator..."
    exit 0
}

if ($OddsApiKey) {
    $OddsApiKey = $OddsApiKey.Trim().Trim('"').Trim("'")
    $env:ODDS_API_KEY = $OddsApiKey
}

Write-Host "Starting NBA Prop Engine UI at http://$HostAddress`:$Port"
& ".\.venv\Scripts\python.exe" ".\server.py" $HostAddress $Port
