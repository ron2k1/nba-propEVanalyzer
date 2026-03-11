# NBA Player Prop Analyzer

A local-first NBA player prop expected value engine with projection modeling, multi-book line scanning, historical backtesting, paper trading, and LLM-powered analysis.

## Highlights

- **Projection engine** with calibrated probability models and per-stat temperature scaling
- **Multi-sportsbook scanning** across BetMGM, DraftKings, and FanDuel
- **Expected value analysis** with confidence-gated picks and closing line value (CLV) tracking
- **Paper trading journal** with forward validation, settlement, and GO-LIVE gate metrics
- **Historical backtesting** against NBA API, Basketball-Reference, and local Kaggle datasets
- **LLM reasoning layer** (local Ollama-first, Claude fallback) for narrative analysis
- **Browser UI** with 7-tab workflow: Dashboard, Lines, Picks, Analyze, Live, Results, Reference
- **CLI + HTTP API** for scripting and integration

## Project Structure

```
server.py        HTTP server (port 8787)
nba_mod.py       CLI entrypoint
core/            Engine modules (projection, EV, odds, tracking, backtesting)
nba_cli/         CLI command handlers
scripts/         Standalone utilities (collection, backfill, quality gate)
web/             Alpine.js browser UI
models/          Calibration configs
data/            Runtime data (gitignored)
docs/            Runbooks and architecture guides
```

## Setup

**Requirements:** Python 3.10+

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Copy and fill in your API keys:

```powershell
Copy-Item .env.example .env
```

| Key | Enables |
|-----|---------|
| `ODDS_API_KEY` | Sportsbook odds, auto sweep, line collection |
| `NEWS_API_KEY` | Injury and news signal features |
| `ANTHROPIC_API_KEY` | Claude LLM fallback (optional) |
| `LLM_PROVIDER_ORDER` | `ollama_first` (default) or `claude_first` |

## Run

**Web UI:**

```powershell
.\.venv\Scripts\python.exe server.py
# Open http://127.0.0.1:8787
```

Windows elevated launcher (resolves firewall issues):

```powershell
powershell -ExecutionPolicy Bypass -File ".\run_ui.ps1"
```

**CLI examples:**

```powershell
# Today's games
.\.venv\Scripts\python.exe nba_mod.py games

# Single prop EV
.\.venv\Scripts\python.exe nba_mod.py prop_ev "Anthony Edwards" ORL 1 pts 25.5 -110 -110 0 MIN

# Auto sweep across books
.\.venv\Scripts\python.exe nba_mod.py auto_sweep "Anthony Edwards" MIN ORL 1 pts 0 us "betmgm,draftkings,fanduel" basketball_nba 15

# Best picks today
.\.venv\Scripts\python.exe nba_mod.py best_today 15

# Paper trading
.\.venv\Scripts\python.exe nba_mod.py collect_lines --books betmgm,draftkings,fanduel --stats pts,reb,ast
.\.venv\Scripts\python.exe nba_mod.py paper_settle 2026-03-01
.\.venv\Scripts\python.exe nba_mod.py paper_summary --window-days 14
```

## Backtesting

Run against local historical data for fast, offline evaluation:

```powershell
# Build local index from Kaggle dataset
.\.venv\Scripts\python.exe scripts\index_local_data.py --input-dir "NBA Database (1947 - Present)" --output "data\reference\kaggle_nba\index.pkl"

# Run backtest
.\.venv\Scripts\python.exe nba_mod.py backtest 2022-01-01 2022-12-31 --model full --local --save
```

Also supports Basketball-Reference ingestion and SportsDataIO backfill. See `docs/runbooks/backtest.md` for details.

## Quality Gate

A pre-commit hook runs automated checks before every commit. Install it once:

```powershell
# Bash (Git Bash / Linux / macOS)
bash scripts/install-hooks.sh

# PowerShell (Windows)
powershell scripts/install-hooks.ps1
```

Run manually at any time:

```powershell
.\.venv\Scripts\python.exe scripts\quality_gate.py
```

## LLM Analysis

Optional LLM-powered narrative reasoning for individual props. Defaults to local Ollama, falls back to Claude API if configured.

```powershell
.\.venv\Scripts\python.exe nba_mod.py llm_analyze "Anthony Edwards" MIN ORL 1 pts 25.5 -110 -110
```

Runtime LLM calls generate analysis output only — they never modify source code.

## Notes

- **Private repo.** Do not commit `.env` or API keys.
- Runtime data (`data/`, `.nba_cache/`) is gitignored.
- All CLI commands output parseable JSON on the last stdout line.
- See `docs/` for architecture guides, runbooks, and operational procedures.
