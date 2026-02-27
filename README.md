# NBA Player Prop Betting Analyzer

Local NBA prop analysis app with:

- Python backend (`server.py`) on `http://127.0.0.1:8787`
- Vanilla JS/HTML/CSS frontend in `web/`
- CLI entrypoint `nba_mod.py`
- EV analysis, auto line sweep, live projections, tracking/settlement, starter accuracy, and LLM reasoning

## What Is In This Repo

```
server.py          ← HTTP server entrypoint (port 8787)
nba_mod.py         ← CLI entrypoint
run_ui.ps1         ← Windows elevated launcher
requirements.txt
.env / .env.example
CLAUDE.md          ← authoritative architecture reference

core/              ← all 19 engine modules (data, projection, EV, ML, tracking…)
  __init__.py
  nba_data_collection.py   NBA Stats API + Odds API + caching
  nba_data_prep.py         facade → nba_prep_projection + nba_prep_usage
  nba_prep_projection.py   projection logic
  nba_prep_usage.py        usage-adjustment math
  nba_ev_engine.py         EV / distribution math (Normal / Poisson / reference)
  nba_prop_engine.py       prop EV, auto sweep, live projection
  nba_parlay_engine.py     parlay EV
  nba_model_training.py    facade → ev / prop / parlay / ML
  nba_model_ml_training.py ridge calibrator + ML pipeline
  nba_backtest.py          historical backtesting (nba / bref / local sources)
  nba_bet_tracking.py      JSONL journal, settlement, CLV tracking
  nba_bref_data.py         Basketball-Reference curated JSONL reader
  nba_local_stats.py       Kaggle pickle index reader (zero API calls)
  nba_minutes_model.py     minutes multiplier model
  nba_line_store.py        line snapshots, CLV, stale detection, alerts
  nba_llm_engine.py        LLM reasoning (Ollama-first, Claude fallback)
  nba_injury_news.py       injury signal ingestion + usage overlay
  nba_starter_accuracy.py  historical starter prop accuracy eval

nba_cli/           ← CLI command handlers (import from core.*)
  router.py, ev_commands.py, core_commands.py, tracking_commands.py
  ml_commands.py, llm_commands.py, line_commands.py, shared.py

scripts/           ← standalone workflow scripts (import from core.*)
  betmgm_scan.py, collect_lines.py, injury_monitor.py
  bref_ingest.py, index_local_data.py, quality_gate.py

scratch/           ← exploratory / daily-briefing scripts
web/               ← browser UI (index.html, app.js, styles.css)
docs/              ← architecture.md (points to CLAUDE.md)
tests/             ← placeholder for future test suite
```

**Import policy:** all engine logic lives in `core/`. External callers (`nba_cli/`, `scripts/`, `scratch/`) use `from core.nba_X import Y`. Modules within `core/` use relative imports (`from .nba_X import Y`).

## Requirements

- Python 3.10+
- Windows PowerShell commands below (works cross-platform with equivalent Python commands)

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

## Environment Variables

Copy template:

```powershell
Copy-Item .env.example .env
```

Optional keys:

- `ODDS_API_KEY`: enables sportsbook odds, auto sweep, starter accuracy
- `NEWS_API_KEY`: enables injury/news features
- `ANTHROPIC_API_KEY`: optional Claude access
- `LLM_PROVIDER_ORDER`: `ollama_first` (default) or `claude_first`

Shell note:
- PowerShell: `$env:LLM_PROVIDER_ORDER="ollama_first"`
- `cmd.exe`: `set LLM_PROVIDER_ORDER=ollama_first`

`core/nba_llm_engine.py` defaults to local Ollama first (`http://localhost:11434`, model `gpt-oss:20b`), then Claude fallback.

If you see `WinError 10013` on Odds API calls in Windows CLI, allow Python through Windows Firewall
or launch with `run_ui.ps1` (it relaunches elevated).

Note: runtime LLM calls only generate analysis output; they do not modify source code files.

## Run

Run server directly:

```powershell
.\.venv\Scripts\python.exe .\server.py
```

Open:

- `http://127.0.0.1:8787`

Windows helper launcher:

```powershell
powershell -ExecutionPolicy Bypass -File ".\run_ui.ps1"
```

## Quick CLI Smoke Checks

```powershell
.\.venv\Scripts\python.exe .\nba_mod.py games
.\.venv\Scripts\python.exe .\nba_mod.py player_lookup "anthony edwards" 10
.\.venv\Scripts\python.exe .\nba_mod.py prop_ev "Anthony Edwards" ORL 1 pts 25.5 -110 -110 0 MIN
.\.venv\Scripts\python.exe .\nba_mod.py auto_sweep "Anthony Edwards" MIN ORL 1 pts 0 us "draftkings,fanduel" basketball_nba 15
.\.venv\Scripts\python.exe .\nba_mod.py live_projection "Anthony Edwards" MIN ORL 1 pts
.\.venv\Scripts\python.exe .\nba_mod.py starter_accuracy
.\.venv\Scripts\python.exe .\scripts\betmgm_scan.py --games 6 --top 10 --min-edge 0.03
```

## Historical Dataset Ingest (Basketball-Reference)

Download and curate local game/boxscore files:

```powershell
.\.venv\Scripts\python.exe .\scripts\bref_ingest.py --date-from 2026-02-01 --date-to 2026-02-25
```

Strict mode (fails if zero games/rows are ingested):

```powershell
.\.venv\Scripts\python.exe .\scripts\bref_ingest.py --date-from 2026-02-01 --date-to 2026-02-25 --fail-on-empty
```

Or ingest a full season window:

```powershell
.\.venv\Scripts\python.exe .\scripts\bref_ingest.py --season 2026
```

Then run backtest against local curated files:

```powershell
.\.venv\Scripts\python.exe .\nba_mod.py backtest 2026-02-19 2026-02-25 --model full --save --data-source bref
```

Optional custom curated folder:

```powershell
.\.venv\Scripts\python.exe .\nba_mod.py backtest 2026-02-19 2026-02-25 --model full --data-source bref --bref-dir "C:\path\to\data\bref\curated"
```

## Local Historical Backtests (Kaggle)

Supported dataset:
- https://www.kaggle.com/datasets/eoinamoore/historical-nba-data-and-player-box-scores

Build local index:

```powershell
.\.venv\Scripts\python.exe .\scripts\index_local_data.py --input-dir "NBA Database (1947 - Present)" --output "data\reference\kaggle_nba\index.pkl"
```

Run local-mode backtest:

```powershell
.\.venv\Scripts\python.exe .\nba_mod.py backtest 2022-01-01 2022-12-31 --model full --local --save
```

If the requested date range exceeds local index coverage, backtest falls back to NBA API and prints a warning.

## Quality Gate (Recommended)

Run automated checks to catch regressions and common AI-scaffold artifacts:

```powershell
.\.venv\Scripts\python.exe .\scripts\quality_gate.py
```

Include slower LLM smoke tests:

```powershell
.\.venv\Scripts\python.exe .\scripts\quality_gate.py --full
```

This does:
- Python compile checks
- JS syntax check (`web/app.js`) if Node is installed
- Pattern scan for known hallucination/stub anti-patterns
- Optional end-to-end LLM CLI smoke tests (`--full`)

Automation:
- GitHub Action quick gate runs on push/PR: [.github/workflows/quality-gate.yml](.github/workflows/quality-gate.yml)
- Repo skill definition for repeated use: [skills/quality-gate/SKILL.md](skills/quality-gate/SKILL.md)

LLM commands:

```powershell
.\.venv\Scripts\python.exe .\nba_mod.py llm_analyze "Anthony Edwards" MIN ORL 1 pts 25.5 -110 -110
.\.venv\Scripts\python.exe .\nba_mod.py llm_injury MIN 24
.\.venv\Scripts\python.exe .\nba_mod.py llm_line "Anthony Edwards" pts 25.5 27.2
```

## API Endpoints (Primary)

- `GET /api/health`
- `GET /api/games`
- `GET /api/teams`
- `GET /api/players`
- `GET /api/player_lookup?q=<name>&limit=<n>`
- `GET /api/team_players?teamIds=<comma_csv>`
- `GET /api/odds?...`
- `GET /api/odds_live?...`
- `GET /api/best_today?limit=<n>&date=<yyyy-mm-dd_optional>`
- `GET /api/results_yesterday?limit=<n>&date=<yyyy-mm-dd_optional>`
- `GET /api/settle_yesterday?date=<yyyy-mm-dd_optional>`
- `GET /api/starter_accuracy?date=<yyyy-mm-dd_optional>&bookmakers=<csv>&regions=us&sport=basketball_nba&modelVariant=full|simple`
- `POST /api/prop_ev`
- `POST /api/prop_ev_ml`
- `POST /api/auto_sweep`
- `POST /api/live_projection`
- `POST /api/parlay_ev`
- `POST /api/llm_analyze`

## Notes For Cloning/Sharing

- The repo is safe to keep private on GitHub.
- Do not commit `.env` or API keys.
- Runtime data/cache folders are git-ignored (`data/`, `.nba_cache/`, `.tmp/`).
- If you later make access broader, users only need this repo + dependencies + optional API keys.
- Runtime LLM analysis does not modify source code files. Code changes remain a developer workflow task.
