# NBA Prop EV Engine — Project Overview

## Mission

Private NBA player-prop expected value engine for paper-trading and GO-LIVE betting.

**Core insight:** The model is already near the realistic accuracy ceiling (~57-58% hit rate). The remaining edge comes from **CLV filtering** (selecting the right bets), not from squeezing accuracy. A 55% model that only bets CLV-positive lines can produce +5-8% ROI.

## Goals

1. Pass the GO-LIVE gate by 2026-03-14 (paper-trading 14-day window)
2. Maintain real-line hit rate > 52.4% (breakeven at -110)
3. Achieve positive real-line ROI on pts, ast, pra (reb is under review)
4. Enforce CLV gate: `clvLine > 0` AND `clvOddsPct > 0` for high-quality signals

## Constraints

- **Stat whitelist:** pts, reb, ast, pra only (fg3m/stl/blk/tov blocked — Poisson structural bias)
- **Book priority:** BetMGM > DraftKings > FanDuel
- **Confidence gate:** Block 40–60% probability bins (unprofitable after vig)
- **Min edge:** 0.05 (no-vig); reb requires 0.08
- **Odds API credits:** 20K/month (~$30); don't spend on blocked stats
- **No-lookahead:** `date_to` in backtests must be strictly before today
- **Do not chase:** BRef/RAPM/Bayesian priors, or try to beat 58% hit rate — those are diminishing returns

## Architecture

- `core/` — all engine modules (21 files); relative imports within
- `nba_cli/` — CLI handlers; absolute imports from core
- `server.py` — Flask-like HTTP server on port 8787
- `nba_mod.py` — CLI dispatcher → `nba_cli/router.py`
- Data: `data/line_history/`, `data/decision_journal/`, `data/reference/odds_history/`
- Models: `models/prob_calibration.json` (temperature-scaled per stat)

## Key Metrics (Baseline: 2026-01-26 to 2026-02-25, 30d)

| Metric | Value |
|--------|-------|
| Real-line hit rate | 55.0% |
| Breakeven at -110 | 52.4% |
| Real-line ROI | +0.088% (barely positive) |
| Real-line coverage | 26% (8,097 / 30,728) |
| pts ROI | +2.6% |
| ast ROI | +1.7% |
| pra ROI | -0.1% |
| reb ROI | -5.9% (under review) |
| Avg Brier | 0.233 (target < 0.235) |
