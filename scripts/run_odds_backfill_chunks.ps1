# Run historical odds backfill in chunks (sequential or parallel).
#
# Usage:
#   Sequential (one terminal, runs all chunks one after another):
#     .\scripts\run_odds_backfill_chunks.ps1
#
#   Print commands only (copy each into a separate PowerShell window to run in parallel):
#     .\scripts\run_odds_backfill_chunks.ps1 -PrintOnly
#
#   Custom range / chunk size:
#     .\scripts\run_odds_backfill_chunks.ps1 -DateFrom "2025-10-21" -DateTo "2026-02-28" -ChunkDays 7 -MaxRequestsPerChunk 2000
#
# After backfill (either way), run once:
#   .\.venv\Scripts\python.exe nba_mod.py odds_build_closes 2025-10-21 2026-02-28

param(
    [string]$DateFrom = "2025-10-21",
    [string]$DateTo   = "2026-02-28",
    [int]$ChunkDays   = 14,
    [int]$MaxRequestsPerChunk = 2500,
    [switch]$PrintOnly,
    [switch]$SkipBuildCloses
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot + "\.."
$Python    = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$NbaMod    = Join-Path $RepoRoot "nba_mod.py"

if (-not (Test-Path $Python)) {
    Write-Error "Python not found at $Python"
}
if (-not (Test-Path $NbaMod)) {
    Write-Error "nba_mod.py not found at $NbaMod"
}

$fromDate = [DateTime]::ParseExact($DateFrom, "yyyy-MM-dd", $null)
$toDate   = [DateTime]::ParseExact($DateTo,   "yyyy-MM-dd", $null)

# Build chunk list: [ (from, to), ... ]
$chunks = @()
$current = $fromDate
while ($current -le $toDate) {
    $chunkEnd = $current.AddDays($ChunkDays - 1)
    if ($chunkEnd -gt $toDate) { $chunkEnd = $toDate }
    $chunks += @{
        From = $current.ToString("yyyy-MM-dd")
        To   = $chunkEnd.ToString("yyyy-MM-dd")
    }
    $current = $chunkEnd.AddDays(1)
}

Write-Host "[backfill-chunks] Range $DateFrom -> $DateTo | ChunkDays=$ChunkDays | Chunks=$($chunks.Count) | MaxRequestsPerChunk=$MaxRequestsPerChunk"
Write-Host ""

if ($PrintOnly) {
    Write-Host "Run each command below in a SEPARATE PowerShell window (same repo folder). All use --resume and the same DB."
    Write-Host ""
    $i = 0
    foreach ($c in $chunks) {
        $i++
        $cmd = "Set-Location `"$RepoRoot`"; .\.venv\Scripts\python.exe nba_mod.py odds_backfill $($c.From) $($c.To) --books betmgm,draftkings,fanduel --stats pts,ast,pra --offset-minutes 60 --max-requests $MaxRequestsPerChunk --resume"
        Write-Host "# Chunk $i of $($chunks.Count) ($($c.From) -> $($c.To))"
        Write-Host $cmd
        Write-Host ""
    }
    Write-Host "# After all chunks finish, run once (in any window):"
    Write-Host "Set-Location `"$RepoRoot`"; .\.venv\Scripts\python.exe nba_mod.py odds_build_closes $DateFrom $DateTo"
    exit 0
}

# Sequential: run each chunk in this window
Set-Location $RepoRoot
$chunkNum = 0
foreach ($c in $chunks) {
    $chunkNum++
    Write-Host "[backfill-chunks] Chunk $chunkNum / $($chunks.Count): $($c.From) -> $($c.To)" -ForegroundColor Cyan
    & $Python $NbaMod odds_backfill $c.From $c.To --books betmgm,draftkings,fanduel --stats pts,ast,pra --offset-minutes 60 --max-requests $MaxRequestsPerChunk --resume
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Chunk $chunkNum exited with $LASTEXITCODE; continuing next chunk."
    }
    Write-Host ""
}

if (-not $SkipBuildCloses) {
    Write-Host "[backfill-chunks] Building closing lines for $DateFrom -> $DateTo ..." -ForegroundColor Cyan
    & $Python $NbaMod odds_build_closes $DateFrom $DateTo
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[backfill-chunks] Done. Run 'nba_mod.py odds_coverage --by-date $DateFrom $DateTo' to verify." -ForegroundColor Green
    }
} else {
    Write-Host "[backfill-chunks] Skipping odds_build_closes ( -SkipBuildCloses ). Run manually: nba_mod.py odds_build_closes $DateFrom $DateTo" -ForegroundColor Yellow
}
