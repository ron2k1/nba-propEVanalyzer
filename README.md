# NBA Player Prop Betting Analyzer

Local NBA prop analysis app with:

- Python backend (`server.py`) on `http://127.0.0.1:8787`
- Vanilla JS/HTML/CSS frontend in `web/`
- CLI entrypoint `nba_mod.py`
- EV analysis, auto line sweep, live projections, tracking/settlement, starter accuracy, and LLM reasoning

## What Is In This Repo

- `server.py`: API wrapper + static file server
- `nba_mod.py`: CLI JSON dispatcher
- `nba_cli/`: modular command handlers
- `nba_prop_engine.py`, `nba_ev_engine.py`, `nba_parlay_engine.py`: core modeling/EV logic
- `nba_starter_accuracy.py`: historical starter prop accuracy run
- `nba_llm_engine.py`: Ollama-first (`gpt-oss:20b`) LLM reasoning with Claude fallback
- `web/`: frontend UI

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
- `ANTHROPIC_API_KEY`: optional LLM fallback if Ollama is unavailable

`nba_llm_engine.py` tries local Ollama first (`http://localhost:11434`, model `gpt-oss:20b`), then Anthropic fallback.

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
```

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
