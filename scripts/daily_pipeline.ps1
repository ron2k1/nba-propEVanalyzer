#!/usr/bin/env pwsh
# daily_pipeline.ps1 — Automated daily NBA signal collection
# Schedule via Windows Task Scheduler: twice daily (11am + 5pm)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File ".\scripts\daily_pipeline.ps1"
#   powershell -ExecutionPolicy Bypass -File ".\scripts\daily_pipeline.ps1" -SettleOnly

param(
    [switch]$SettleOnly,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogFile = Join-Path $ProjectRoot "data\daily_pipeline.log"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

# Load .env
$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
        }
    }
}

Set-Location $ProjectRoot
Log "=== Daily pipeline started ==="

# Step 1: Settle yesterday's picks
Log "Settling yesterday..."
if (-not $DryRun) {
    & $Python nba_mod.py paper_settle 2>&1 | Out-String | ForEach-Object { Log $_ }
}

if ($SettleOnly) {
    Log "=== Settle-only mode, done ==="
    exit 0
}

# Step 2: Collect lines
Log "Collecting lines (pts,ast)..."
if (-not $DryRun) {
    & $Python nba_mod.py collect_lines --books betmgm,draftkings,fanduel --stats pts,ast 2>&1 | Out-String | ForEach-Object { Log $_ }
}

# Step 3: Bridge lines (generate signals)
Log "Bridging lines..."
if (-not $DryRun) {
    & $Python nba_mod.py line_bridge --books betmgm,draftkings,fanduel --stats pts,ast 2>&1 | Out-String | ForEach-Object { Log $_ }
}

# Step 4: Summary
Log "Generating paper summary..."
if (-not $DryRun) {
    $summary = & $Python nba_mod.py paper_summary --window-days 30 2>&1 | Out-String
    Log $summary
}

Log "=== Daily pipeline complete ==="
