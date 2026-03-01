# Paste into PowerShell to run odds coverage + 60d backtest and show the result.
# Run from repo root:  cd "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
#
# If PowerShell is stuck (can't type), press Ctrl+C to stop, then run this script again.
# The backtest runs with live output; when it finishes we read the saved JSON.

Set-Location "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"

Write-Host "=== Odds coverage ===" -ForegroundColor Cyan
.\.venv\Scripts\python.exe nba_mod.py odds_coverage

Write-Host "`n=== 60d backtest (local + real lines) — wait for it to finish, output streams below ===" -ForegroundColor Cyan
.\.venv\Scripts\python.exe nba_mod.py backtest 2025-12-27 2026-02-25 --model full --local --odds-source local_history --save

$resultPath = "data\backtest_results\2025-12-27_to_2026-02-25_full_local.json"
if (Test-Path $resultPath) {
    Write-Host "`n=== Backtest summary (from saved file) ===" -ForegroundColor Green
    $j = Get-Content $resultPath -Raw | ConvertFrom-Json
    $r = $j.reports.full
    if ($r) {
        $real = $r.realLineSamples
        $miss = $r.missingLineSamples
        $pct = if ($real + $miss -gt 0) { [math]::Round(100 * $real / ($real + $miss), 1) } else { 0 }
        Write-Host "realLineSamples: $real | missingLineSamples: $miss | Coverage: $pct%"
        if ($r.roiReal) { Write-Host "roiReal: $($r.roiReal.roiPctPerBet)% (bets: $($r.roiReal.betsPlaced), hitRate: $($r.roiReal.hitRatePct)%)" }
    }
} else {
    Write-Host "`n(Saved file not found; backtest may have failed or not saved.)" -ForegroundColor Yellow
}
