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

## Discord Notifications (scripts/discord_notify.py)

Scheduled tasks send rich Discord embeds after each run via webhook.

### Setup

Add to `.env`:
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN
```

Create a webhook: Discord Server Settings → Integrations → Webhooks → New Webhook → Copy URL.

### Notification Types

| Type | Trigger | Color | Content |
|---|---|---|---|
| Morning Summary | `scheduled_settle.py` success | Green/Orange | Gate status, metrics (sample/hit/ROI/CLV), settlement counts |
| Evening Picks | `scheduled_pipeline.py` full run success | Green | Top policy-qualified picks with EV, projection, book |
| Failure Alert | Any task failure | Red | Task name, error message, error class |
| Dead-Man Alert | `/api/discord_deadman` when stale tasks exist | Orange | Stale task names, last success time, threshold |
| Collect-Only Failure | `scheduled_pipeline.py --collect-only` failure | Red | Failed step + error (success is silent) |

### Endpoints

```
GET http://127.0.0.1:8787/api/discord_test     # send a test embed
GET http://127.0.0.1:8787/api/discord_deadman   # check health + alert if stale
```

### Behavior

- Notifications are **non-fatal**: webhook errors are logged to stderr but do not change the task exit code.
- `collect_only` success does NOT send a notification (too frequent — every 2h). Only failures are reported.
- `full_pipeline` and `morning_settle` always notify on completion (success or failure).
- Dead-man alerts must be triggered externally (e.g., a scheduled task calling `/api/discord_deadman`).

## Discord Bot (scripts/discord_bot.py)

Interactive slash commands for querying the pipeline from Discord.

### Setup

1. Create a bot at [Discord Developer Portal](https://discord.com/developers/applications):
   - New Application → Bot tab → Reset Token → copy token
   - OAuth2 → URL Generator → select `bot` + `applications.commands` → select `Send Messages` + `Embed Links`
   - Copy the generated URL and open it to invite the bot to your server

2. Add to `.env`:
```
DISCORD_BOT_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=123456789012345678   # optional — restricts commands to one channel
```

3. Install dependency and run:
```powershell
.\.venv\Scripts\python.exe -m pip install discord.py
.\.venv\Scripts\python.exe scripts\discord_bot.py
```

### Slash Commands

| Command | Description | API Endpoint |
|---|---|---|
| `/picks` | Today's top policy-qualified plays | `/api/best_today?limit=15` |
| `/gate` | Current GO-LIVE gate status | `/api/journal_gate?windowDays=14` |
| `/summary` | Paper trading summary (14d) | `/api/paper_summary?windowDays=14` |
| `/health` | Ops health + task staleness | `/api/ops_health` |

### Architecture

- Bot is a **thin presentation layer** — all data comes from the local API server
- Requires `server.py` to be running (default `http://127.0.0.1:8787`)
- Override with `API_BASE_URL` env var if needed
- Commands use `interaction.response.defer()` since API calls may take a few seconds
- Slash commands sync automatically on bot startup (`tree.sync()`)

### Running as a Service

The bot is a long-running process. Options:
- **Manual:** run in a terminal window
- **Task Scheduler:** create a task that runs `discord_bot.py` at logon (no schedule trigger)
- **PM2/NSSM:** process managers for auto-restart on crash

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| NBAFullPipeline 0xC000013A | 5PM collision with legacy task | Run `-UninstallLegacy` |
| NBAFullPipeline 0xC0000138 | DLL not found (python env issue) | Check `.venv` exists, reinstall |
| `ops_health` returns 404 | Server running old code | Restart server |
| `stale: true` for a task | No successful run within threshold | Check task log in `data/logs/` |
| 503 errors in collect_only | Odds API transient failure | Auto-retried; check quota |
| Discord notifications not sending | `DISCORD_WEBHOOK_URL` not set | Add to `.env`, restart server |
| Discord embed empty/wrong | Pipeline step returned no data | Check `scheduled_runs.jsonl` for step results |
| `discord_test` returns 404 | Server running old code | Restart server |
| Bot commands not appearing | Slash commands not synced | Restart bot; check `on_ready` log |
| Bot returns "Connection failed" | API server not running | Start `server.py` first |
| `DISCORD_BOT_TOKEN not set` | Missing env var | Add `DISCORD_BOT_TOKEN` to `.env` |
| `discord.py not installed` | Missing dependency | `pip install discord.py` |
