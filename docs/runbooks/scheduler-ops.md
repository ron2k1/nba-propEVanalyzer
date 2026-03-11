# Scheduler & Ops Runbook

## Canonical Tasks (install_tasks.ps1)

| Task | Schedule | Script | Purpose |
|---|---|---|---|
| NBAMorningSettle | 10 AM daily | `scheduled_settle.py` | paper_settle + paper_summary |
| NBASnapshotCollection | every 2h (10AM–10PM) | `scheduled_pipeline.py --collect-only` | accumulate line snapshots |
| NBADenseCollector | 3 PM ET daily | `dense_collector.py` | near-tipoff dense collection |
| NBAFullPipeline | 5 PM daily | `scheduled_pipeline.py` | collect + roster_sweep + best_today |
| NBABridgeAndBuild | 11 PM ET daily | `line_bridge` + `odds_build_closes` | JSONL→SQLite + closing lines |

### Install / Uninstall

```powershell
# Install all (run as Administrator)
powershell -ExecutionPolicy Bypass -File .\scripts\tasks\install_tasks.ps1

# Remove legacy/duplicate tasks
powershell -ExecutionPolicy Bypass -File .\scripts\tasks\install_tasks.ps1 -UninstallLegacy

# Remove canonical tasks
powershell -ExecutionPolicy Bypass -File .\scripts\tasks\install_tasks.ps1 -Uninstall
```

Legacy tasks removed by `-UninstallLegacy`: `NBA_DailyPipeline_AM`, `NBA_DailyPipeline_PM`, `NBA_DailyScan_6PM`, `NBA_SettleAM`, `NBAData-AutoCheck-Test`, `NBA-CollectLines`.

## Ops Event Layer (scripts/ops_events.py)

All scheduled scripts emit structured events to `data/logs/scheduled_runs.jsonl`:

- `run_started` — emitted when a task begins, lists planned steps
- `step_completed` — emitted per successful step
- `run_succeeded` / `run_failed` — final outcome with duration and error details

### Error Classification

Errors are classified automatically:

- **transient** — 502, 503, 429, timeout, rate limit, connection errors → auto-retried (2 retries, 5s/10s exponential backoff)
- **permanent** — 401, 403, KeyError, TypeError, ImportError → raised immediately, no retry
- **unknown** — unclassified → raised immediately

### /api/ops_health

Returns per-task health from `scheduled_runs.jsonl`:

```
GET http://127.0.0.1:8787/api/ops_health
```

Response fields per task:
- `lastSuccess` / `lastFailure` — ISO timestamps
- `lastError` / `lastErrorClass` — most recent error details
- `runsLast24h` / `failuresLast24h` — counts
- `stale` — true if no success within threshold (e.g., 2.5h for collect_only, 30h for daily tasks)
- `healthy` — top-level bool, false if any task is stale

**Server restart required** after code changes to expose new endpoints.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| NBAFullPipeline 0xC000013A | 5PM collision with legacy task | Run `-UninstallLegacy` |
| NBAFullPipeline 0xC0000138 | DLL not found (python env issue) | Check `.venv` exists, reinstall |
| `ops_health` returns 404 | Server running old code | Restart server |
| `stale: true` for a task | No successful run within threshold | Check task log in `data/logs/` |
| 503 errors in collect_only | Odds API transient failure | Auto-retried; check quota |
