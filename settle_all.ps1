# settle_all.ps1 — Settle all pending paper-trade entries (requires admin elevation)
# Usage: Right-click → Run with PowerShell as Administrator
#   or:  powershell -ExecutionPolicy Bypass -File .\settle_all.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    throw "Virtual environment not found at .venv\Scripts\python.exe"
}

$python = ".\.venv\Scripts\python.exe"

# Dates with known unsettled entries
$dates = @(
    "2026-02-25",
    "2026-02-26",
    "2026-02-27",
    "2026-02-28",
    "2026-03-01",
    "2026-03-02"
)

Write-Host "=== Settling all pending paper trades ===" -ForegroundColor Cyan
Write-Host ""

foreach ($d in $dates) {
    Write-Host "--- Settling $d ---" -ForegroundColor Yellow
    & $python nba_mod.py paper_settle $d
    Write-Host ""
    Start-Sleep -Seconds 1
}

Write-Host "=== Done. Checking remaining unsettled ===" -ForegroundColor Cyan
Write-Host ""

# Quick count of remaining unsettled
& $python -c @"
import json
with open('data/prop_journal.jsonl', encoding='utf-8') as f:
    entries = [json.loads(line) for line in f if line.strip()]
unsettled = [e for e in entries if e.get('result') is None]
print(f'Remaining unsettled: {len(unsettled)}')
if unsettled:
    from collections import Counter
    for d, c in sorted(Counter(e.get('pickDate') for e in unsettled).items()):
        print(f'  {d}: {c}')
"@

Write-Host ""
Write-Host "Press any key to exit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
