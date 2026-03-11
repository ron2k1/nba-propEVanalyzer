#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Register NBA prop engine scheduled tasks in Windows Task Scheduler.

.DESCRIPTION
  Creates five canonical tasks:
    1. NBASnapshotCollection  - collect odds every 2h (10AM-10PM)
    2. NBAFullPipeline        - collect + sweep + best daily at 5PM
    3. NBAMorningSettle       - paper_settle + paper_summary daily at 10AM
    4. NBADenseCollector      - dense near-tipoff collection daily at 3PM ET
    5. NBABridgeAndBuild      - nightly JSONL-to-SQLite bridge + closing lines at 11PM ET

  Run this script once as Administrator:
    powershell -ExecutionPolicy Bypass -File .\scripts\tasks\install_tasks.ps1

  To uninstall canonical tasks:
    powershell -ExecutionPolicy Bypass -File .\scripts\tasks\install_tasks.ps1 -Uninstall

  To remove legacy/duplicate tasks (safe — only removes known old names):
    powershell -ExecutionPolicy Bypass -File .\scripts\tasks\install_tasks.ps1 -UninstallLegacy

  Recommended first-time setup (clean slate):
    powershell -ExecutionPolicy Bypass -File .\scripts\tasks\install_tasks.ps1 -UninstallLegacy
    powershell -ExecutionPolicy Bypass -File .\scripts\tasks\install_tasks.ps1
#>
param(
    [switch]$Uninstall,
    [switch]$UninstallLegacy
)

$ErrorActionPreference = "Stop"
$taskDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ── Canonical tasks (this script is the sole source of truth) ─────────────────
$tasks = @(
    @{ Name = "NBASnapshotCollection"; Xml = "$taskDir\task_collect.xml" },
    @{ Name = "NBAFullPipeline";       Xml = "$taskDir\task_pipeline.xml" },
    @{ Name = "NBAMorningSettle";      Xml = "$taskDir\task_settle.xml" },
    @{ Name = "NBADenseCollector";     Xml = "$taskDir\task_dense_collect.xml" },
    @{ Name = "NBABridgeAndBuild";     Xml = "$taskDir\task_bridge_build.xml" }
)

# ── Legacy tasks to remove (superseded by canonical tasks) ────────────────────
$legacyTasks = @(
    "NBA_DailyPipeline_AM",    # superseded by NBAMorningSettle + NBASnapshotCollection
    "NBA_DailyPipeline_PM",    # superseded by NBAFullPipeline (was causing 5PM crash)
    "NBA_DailyScan_6PM",       # superseded by NBADenseCollector
    "NBA_SettleAM",            # superseded by NBAMorningSettle
    "NBAData-AutoCheck-Test",  # dead test task (echo hi)
    "NBA-CollectLines"         # old name variant
)

# ── Uninstall legacy tasks ────────────────────────────────────────────────────
if ($UninstallLegacy) {
    Write-Host "Removing legacy/duplicate tasks..." -ForegroundColor Cyan
    $removed = 0
    foreach ($name in $legacyTasks) {
        try {
            $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if ($existing) {
                Unregister-ScheduledTask -TaskName $name -Confirm:$false
                Write-Host "  [REMOVED] $name" -ForegroundColor Green
                $removed++
            } else {
                Write-Host "  [SKIP]    $name (not found)" -ForegroundColor DarkGray
            }
        } catch {
            Write-Host "  [SKIP]    $name (not found)" -ForegroundColor DarkGray
        }
    }
    Write-Host "`nRemoved $removed legacy task(s).`n" -ForegroundColor Cyan

    # Show remaining NBA tasks
    $remaining = Get-ScheduledTask | Where-Object { $_.TaskName -like "NBA*" }
    if ($remaining) {
        Write-Host "Remaining NBA tasks:" -ForegroundColor Yellow
        $remaining | Format-Table TaskName, State -AutoSize
    } else {
        Write-Host "No NBA tasks remaining." -ForegroundColor Yellow
    }
    exit 0
}

# ── Uninstall canonical tasks ─────────────────────────────────────────────────
if ($Uninstall) {
    foreach ($t in $tasks) {
        try {
            Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
            Write-Host "[OK] Removed: $($t.Name)" -ForegroundColor Green
        } catch {
            Write-Host "[SKIP] $($t.Name) not found" -ForegroundColor Yellow
        }
    }
    Write-Host "`nAll canonical NBA tasks removed."
    exit 0
}

# ── Install canonical tasks ───────────────────────────────────────────────────
# Check for legacy collisions first
$collisions = @()
foreach ($name in $legacyTasks) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) { $collisions += $name }
}
if ($collisions.Count -gt 0) {
    Write-Host "WARNING: Legacy tasks still registered (will cause collisions):" -ForegroundColor Red
    foreach ($c in $collisions) {
        Write-Host "  - $c" -ForegroundColor Red
    }
    Write-Host "Run with -UninstallLegacy first to remove them.`n" -ForegroundColor Yellow
}

foreach ($t in $tasks) {
    $xml = Get-Content $t.Xml -Raw
    try {
        Register-ScheduledTask -TaskName $t.Name -Xml $xml -Force | Out-Null
        Write-Host "[OK] Registered: $($t.Name)" -ForegroundColor Green
    } catch {
        Write-Host "[FAIL] $($t.Name): $_" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "=== Registered Tasks ===" -ForegroundColor Cyan
Get-ScheduledTask | Where-Object { $_.TaskName -like "NBA*" } | Format-Table TaskName, State, @{
    Label = "NextRun"
    Expression = { (Get-ScheduledTaskInfo -TaskName $_.TaskName).NextRunTime }
}

Write-Host "To run manually:  schtasks /run /tn NBASnapshotCollection"
Write-Host "To check status:  schtasks /query /tn NBA*"
Write-Host "To uninstall:     powershell -File .\scripts\tasks\install_tasks.ps1 -Uninstall"
Write-Host "Remove legacy:    powershell -File .\scripts\tasks\install_tasks.ps1 -UninstallLegacy"
