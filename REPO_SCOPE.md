# Repository Scope

## What is in this repo

| Path | Purpose |
|------|---------|
| `core/` | Engine logic — EV math, projections, backtesting, odds store, decision journal |
| `nba_cli/` | CLI command handlers; wired through `nba_cli/router.py` |
| `scripts/` | Standalone scripts — backfills, calibration fitting, quality gate |
| `web/` | Static frontend (index.html, app.js, styles.css) |
| `docs/` | Architecture docs, reports, improvement guides |
| `models/prob_calibration.json` | Fitted temperature-scaling calibration (safe to commit, not secret) |
| `nba_mod.py` | CLI entrypoint — dispatches to router |
| `server.py` | HTTP server entrypoint (port 8787) |
| `requirements.txt` | Python dependencies |
| `run_ui.ps1` | Windows launcher with admin elevation |
| `.env.example` | Environment variable template (no secrets) |
| `.github/` | CI workflows |
| `REPO_SCOPE.md` | This file |
| `README.md` | Project overview and quick-start |

## What is intentionally excluded

| Path / Pattern | Reason |
|----------------|--------|
| `.env` | Contains live API keys — never committed |
| `data/` | 1.1 GB of historical odds, backtests, journals — too large, contains PII-adjacent data |
| `*.sqlite` / `*.jsonl` / `*.ndjson` | Runtime databases and line snapshots |
| `models/*.pkl` | Binary serialized ML models — regenerate from source |
| `.nba_cache/` | Disk cache of NBA Stats API responses |
| `logs/` | Runtime application logs |
| `.venv/` | Python virtual environment — recreate with `pip install -r requirements.txt` |
| `.cursor/` | Cursor IDE project rules — local tooling config |
| `.claude/` | Claude Code project metadata — local tooling config |
| `scratch/` | Exploratory one-off scripts, not production code |
| `Win-CodexBar/` | Separate repository (Rust project) — not part of the NBA engine |
| `CLAUDE.md` | Private project instructions for AI assistants |
| `PLAN*.md` / `STATE.md` | Operational planning notes — session artifacts |
| `NBA Database (1947 - Present)/` | Large local reference database |

## Reproducing the data layer

The `data/` directory is never committed. To rebuild:

1. **Odds history**: `nba_mod.py odds_backfill <from> <to> --resume`
2. **Closing lines**: `nba_mod.py odds_build_closes`
3. **Calibration**: `scripts/fit_calibration.py --input <backtest_json> --output models/prob_calibration.json`
4. **Decision journal**: auto-created on first `prop_ev` / `auto_sweep` run

## External dependency: Win-CodexBar

`Win-CodexBar/` is a separate Rust project that lives alongside this repo locally.
It is **not** part of the NBA prop engine and is not tracked here.
If you need it, clone it independently from its own repository.
