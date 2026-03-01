param(
    [string]$TaskName = "NBAData-AutoCheck",
    [string]$RunValueName = "NBADataAutoCheck"
)

$ErrorActionPreference = "Stop"

$taskDeleted = $false
$regDeleted = $false

$runPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

schtasks /Query /TN $TaskName | Out-Null 2>$null
if ($LASTEXITCODE -eq 0) {
    schtasks /Delete /TN $TaskName /F | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $taskDeleted = $true
    }
}

try {
    $existing = Get-ItemProperty -Path $runPath -Name $RunValueName -ErrorAction Stop
    if ($null -ne $existing) {
        Remove-ItemProperty -Path $runPath -Name $RunValueName -Force
        $regDeleted = $true
    }
} catch {
    # No registry startup value found.
}

if (-not $taskDeleted -and -not $regDeleted) {
    Write-Output "No startup registration found for auto-check."
    exit 0
}

Write-Output "Auto-check startup registration removed."
Write-Output "Deleted scheduled task: $taskDeleted"
Write-Output "Deleted registry Run key: $regDeleted"
