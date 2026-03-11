# Scheduler & Ops Runbook

## Canonical Tasks (install_tasks.ps1)

| Task | Schedule | Script | Purpose |
|---|---|---|---|
| NBAMorningSettle | 10 AM daily | `scheduled_settle.py` | paper_settle + paper_summary |
| NBASnapshotCollection | every 2h (10AM–10PM) | `scheduled_pipeline.py --collect-only` | accumulate line snapshots |
| NBADenseCollector | 3 PM ET daily | `dense_collector.py` | near-tipoff dense collection |
| NBAFullPipeline | 5 PM daily | `scheduled_pipeline.py` | collect + roster_sweep + best_today |
| NBABridgeAndBuild | 11 PM ET daily | `line_bridge` + `odds_build_closes` | JSONL→SQLite + closing lines |
| NBADeadmanCheck | every 4h | `scheduled_deadman.py` | dead-man health check + Discord alert |
| NBALineMonitor | every 2h (10AM–10PM) | `monitor_lines.py` | line movement detection + alert |
| NBAInjuryMonitor | every 2h (10AM–10PM) | `monitor_injuries.py` | injury news polling + alert |

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
- `stale` — true if no success within threshold (e.g., 2.5h for collect_only, 5h for deadman_check, 30h for daily tasks)
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
| Dense Collector | `dense_collector.py` completion | Green/Red | Events, windows, API calls, snapshots, bridge+build |
| Failure Alert | Any task failure | Red | Task name, error message, error class |
| Dead-Man Alert | `scheduled_deadman.py` when stale tasks | Orange | Stale task names, last success time, threshold |
| Line Movement | `monitor_lines.py` on significant moves | Purple | Player, stat, previous/current line, delta |
| Injury Alert | `monitor_injuries.py` on new signals | Yellow | Player, team, status, confidence |
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
- Dense collector notifies on completion (start-to-finish run, not per-window).
- Dead-man check runs every 4h automatically; also callable via `/api/discord_deadman`.
- Line monitor runs every 2h; compares against previous state to detect moves >= 1.0 point.
- Injury monitor runs every 2h; only alerts on NEW signals not seen in previous check.

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
DISCORD_GUILD_ID=123456789012345678     # optional — instant slash sync (else ~1h)
DISCORD_OWNER_ID=123456789012345678     # optional — restrict commands to one user
DISCORD_PICKS_CHANNEL_ID=123456789012345678  # optional — auto-post picks here
DISCORD_LEANS_CHANNEL_ID=123456789012345678  # optional — auto-post leans here
```

3. Install dependency and run:
```powershell
.\.venv\Scripts\python.exe -m pip install discord.py
.\.venv\Scripts\python.exe scripts\discord_bot.py
```

### Slash Commands

| Command | Description | Source |
|---|---|---|
| `/picks` | Today's top policy-qualified plays | `/api/best_today?limit=15` |
| `/gate` | Current GO-LIVE gate status | `/api/journal_gate?windowDays=14` |
| `/summary` | Paper trading summary (14d) | `/api/paper_summary?windowDays=14` |
| `/health` | Ops health + task staleness | `/api/ops_health` |
| `/lines` | Check for significant line movements | `monitor_lines.py` (inline) |
| `/injuries` | Injury news for today's teams | `monitor_injuries.py` (inline) |

### Architecture

- Bot is a **thin presentation layer** — most data comes from the local API server
- `/lines` and `/injuries` run monitor logic inline (no API call needed)
- Requires `server.py` to be running for `/picks`, `/gate`, `/summary`, `/health`
- Override with `API_BASE_URL` env var if needed
- Commands use `interaction.response.defer()` since calls may take a few seconds
- Slash commands sync automatically on bot startup (`tree.sync()`)

### Running as a Service

The bot is a long-running process. Options:
- **Manual:** run in a terminal window
- **Task Scheduler:** create a task that runs `discord_bot.py` at logon (no schedule trigger)
- **PM2/NSSM:** process managers for auto-restart on crash

## Event-Driven Monitors

### Line Movement Monitor (scripts/monitor_lines.py)

Detects significant line moves between snapshot intervals.

- **How it works:** Compares current best_today signals against saved state from previous run
- **Thresholds:** Line move >= 1.0 point, odds shift >= 15 cents
- **State file:** `data/logs/line_monitor_state.json`
- **Discord:** Purple embed with player, stat, prev/current line, delta

```powershell
# Manual run
.\.venv\Scripts\python.exe scripts\monitor_lines.py
.\.venv\Scripts\python.exe scripts\monitor_lines.py --threshold 0.5 --dry-run
```

### Injury Monitor (scripts/monitor_injuries.py)

Polls injury news for today's game teams.

- **How it works:** Fetches injury signals for all teams playing today, filters to high-confidence, compares against previous check
- **Thresholds:** Confidence >= 0.60
- **State file:** `data/logs/injury_monitor_state.json`
- **Discord:** Yellow embed with player, team, status, confidence

```powershell
# Manual run
.\.venv\Scripts\python.exe scripts\monitor_injuries.py
.\.venv\Scripts\python.exe scripts\monitor_injuries.py --min-confidence 0.70 --dry-run
```

### Dead-Man Check (scripts/scheduled_deadman.py)

Automated health check that alerts on stale tasks.

- **How it works:** Reads ops health, sends Discord alert if any task hasn't succeeded within its expected interval
- **Frequency:** Every 4 hours
- **Discord:** Orange embed with stale task details (reuses `notify_deadman`)

```powershell
# Manual run
.\.venv\Scripts\python.exe scripts\scheduled_deadman.py
.\.venv\Scripts\python.exe scripts\scheduled_deadman.py --dry-run
```

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
| Line monitor no movements | First run (no previous state) | Run again after next snapshot |
| Injury monitor no signals | No games today or NEWS_API_KEY missing | Check `.env` for `NEWS_API_KEY` |
| Dense collector no Discord | `load_dotenv` missing | Already added in Phase 4 |
