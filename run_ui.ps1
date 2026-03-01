param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8787,
    [string]$OddsApiKey = "",
    [string]$AnthropicApiKey = "",
    [string]$LlmProviderOrder = ""
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
    if (-not $OddsApiKey -and $env:ODDS_API_KEY) {
        $OddsApiKey = "$env:ODDS_API_KEY"
    }
    if (-not $AnthropicApiKey -and $env:ANTHROPIC_API_KEY) {
        $AnthropicApiKey = "$env:ANTHROPIC_API_KEY"
    }
    if (-not $LlmProviderOrder -and $env:LLM_PROVIDER_ORDER) {
        $LlmProviderOrder = "$env:LLM_PROVIDER_ORDER"
    }

    if ($OddsApiKey) {
        # Defensive cleanup in case caller wrapped the key in quotes.
        $OddsApiKey = $OddsApiKey.Trim().Trim('"').Trim("'")
    }
    if ($AnthropicApiKey) {
        $AnthropicApiKey = $AnthropicApiKey.Trim().Trim('"').Trim("'")
    }
    if ($LlmProviderOrder) {
        $LlmProviderOrder = $LlmProviderOrder.Trim().Trim('"').Trim("'")
    }
    $args = @(
        "-ExecutionPolicy", "Bypass",
        "-File", $PSCommandPath,
        "-HostAddress", $HostAddress,
        "-Port", "$Port",
        "-OddsApiKey", "$OddsApiKey",
        "-AnthropicApiKey", "$AnthropicApiKey",
        "-LlmProviderOrder", "$LlmProviderOrder"
    )
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $args | Out-Null
    Write-Host "Relaunching as Administrator..."
    exit 0
}

if ($OddsApiKey) {
    $OddsApiKey = $OddsApiKey.Trim().Trim('"').Trim("'")
    $env:ODDS_API_KEY = $OddsApiKey
}
if ($AnthropicApiKey) {
    $AnthropicApiKey = $AnthropicApiKey.Trim().Trim('"').Trim("'")
    $env:ANTHROPIC_API_KEY = $AnthropicApiKey
}
if ($LlmProviderOrder) {
    $LlmProviderOrder = $LlmProviderOrder.Trim().Trim('"').Trim("'").ToLower()
    $env:LLM_PROVIDER_ORDER = $LlmProviderOrder
}

Write-Host "Starting NBA Prop Engine UI at http://$HostAddress`:$Port"
& ".\.venv\Scripts\python.exe" ".\server.py" $HostAddress $Port
