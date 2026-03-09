# Architecture

## System Overview

Private NBA player-prop EV engine. Python backend + Alpine.js frontend + CLI.

- `server.py` — ThreadingHTTPServer on port 8787, proxies CLI commands via subprocess
- `nba_mod.py` — CLI dispatcher, loads dotenv, routes to `nba_cli/router.py`
- `web/` — Alpine.js v3 frontend with 7 workflow-ordered tabs

## Import Policy

- **Within `core/`:** relative imports — `from .nba_X import Y`
- **Outside `core/`** (nba_cli/, scripts/, scratch/): absolute — `from core.nba_X import Y`
- **Data paths in `core/`:** use `dirname(dirname(__file__))` for repo root
- **New CLI commands:** wire in `nba_cli/router.py` + handler file. Never add logic to `nba_mod.py`

## Module Map

### CLI Entry + Routing
| Module | Purpose |
|--------|---------|
| `nba_mod.py` | Thin entrypoint (dotenv + JSON output + exception boundary) |
| `nba_cli/router.py` | Command dispatcher |
| `nba_cli/shared.py` | Shared constants and player-ID resolver |

### Command Handlers (`nba_cli/`)
| Module | Domain |
|--------|--------|
| `core_commands.py` | Data pulls, player/team lookups, odds, injury |
| `ev_commands.py` | Projection, prop EV, auto sweep, parlay, live projection |
| `backtest_commands.py` | Backtest execution and reporting |
| `tracking_commands.py` | Settlement, results, decision journal, export |
| `journal_commands.py` | Journal querying and reporting |
| `line_commands.py` | Line collection and bridging |
| `scan_commands.py` | Large-scale prop scanning |
| `projection_commands.py` | Projection analysis |
| `odds_commands.py` | Odds collection, closing line building |
| `ml_commands.py` | ML model training and promotion |
| `llm_commands.py` | LLM analysis |
| `ops_commands.py` | Operations utilities |
| `manual_bet_commands.py` | Manual bet tracking |

### Engine Core (`core/`)
| Module | Purpose |
|--------|---------|
| `nba_data_collection.py` | NBA Stats + Odds API fetches, caching, BETTING_POLICY |
| `nba_prep_projection.py` | Projection features, shrinkage, per-bin temps |
| `nba_prep_usage.py` | Usage adjustments |
| `nba_data_prep.py` | Facade for projection + usage |
| `nba_ev_engine.py` | Probability/EV math (Normal, Poisson, reference) |
| `nba_prop_engine.py` | Prop-level assembly, auto sweep, line matching |
| `nba_parlay_engine.py` | Parlay EV |
| `nba_backtest.py` | Historical backtesting (NBA API, BRef, local). BRef provider is implemented but **not currently provisioned** in this workspace (`data/bref/curated/` files are empty). Use `--local` or default NBA API mode instead. |
| `nba_bet_tracking.py` | JSONL journal, settlement, CLV tracking |
| `nba_decision_journal.py` | SQLite signal logger, paper-trading validator, gates |
| `gates.py` | Signal qualification gates, SIGNAL_SPEC |
| `nba_odds_store.py` | SQLite store for historical odds snapshots + closing lines |
| `nba_line_store.py` | Line snapshots, CLV, stale detection, alerts |
| `nba_minutes_model.py` | Minutes multiplier model |
| `nba_model_ml_training.py` | Ridge calibrator + ML pipeline |
| `nba_llm_engine.py` | LLM reasoning (Ollama-first, Claude fallback) |
| `nba_injury_news.py` | Injury signal ingestion + usage overlay |
| `nba_starter_accuracy.py` | Starter prop accuracy eval |
| `nba_local_stats.py` | Kaggle pickle index reader |

### Tier 2 Extension Points
- Pace adjustment: `nba_prep_projection.py` projection assembly
- Residual stdev calibration: `nba_prop_engine.py` + `nba_ev_engine.py`
- Sample-size gate: `nba_prop_engine.py` before EV computation
- CLV/settlement metrics: `nba_bet_tracking.py`
- Season-aware defense: `nba_data_collection.py` (currently hardcodes CURRENT_SEASON)

## Data Layer

| Path | Content | Git |
|------|---------|-----|
| `data/decision_journal/` | SQLite decision journal | Ignored |
| `data/reference/odds_history/` | SQLite odds history | Ignored |
| `data/line_history/` | YYYY-MM-DD.jsonl line snapshots | Ignored |
| `data/backtest_results/` | Backtest output JSONs | Ignored |
| `data/prop_journal.jsonl` | Decision journal flat export | Ignored |
| `models/prob_calibration.json` | Calibration temps | Committed |
| `models/policy_history.json` | Policy checkpoints | Committed |

## What Is Intentionally Excluded From Git

| Path | Reason |
|------|--------|
| `.env` | Live API keys |
| `data/` | 1.1 GB historical odds, backtests, journals |
| `*.sqlite` / `*.jsonl` | Runtime databases |
| `models/*.pkl` | Binary ML models |
| `.nba_cache/` | NBA Stats API response cache |
| `scratch/` | Exploratory scripts |
| `.claude/` / `.claude-flow/` | AI tooling config |
