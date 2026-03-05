# Staged Backtest Plan: 7d → 14d → 30d → 60d

## Overview

Run the current model across four nested windows to assess stability and sample-size effects. Use a fixed end date (last date with closing-line coverage) so windows are comparable.

## Windows

| Window | Start | End | Purpose |
|--------|-------|-----|---------|
| 7-day | (end - 6 days) | end | Recent regime; high variance, quick signal |
| 14-day | (end - 13 days) | end | Medium sample; balance recency vs stability |
| 30-day | (end - 29 days) | end | Full sample; primary metrics, calibration |
| 60-day | (end - 59 days) | end | Trend/stability; not for GO/NO-GO; logged automatically |

**Note:** `date_to` must be strictly before today (no-lookahead). Adjust dates to match your local index and closing-line coverage.

## Commands

```powershell
# 1. Verify coverage
.\.venv\Scripts\python.exe nba_mod.py odds_coverage --by-date 2026-01-26 2026-02-27

# 2. Run all four backtests (7d / 14d / 30d manual; 60d one-command + auto-log)
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-02-21 2026-02-27 --model full --local --odds-source local_history --save
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-02-14 2026-02-27 --model full --local --odds-source local_history --save
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-01-29 2026-02-27 --model full --local --odds-source local_history --save

# 3. 60d backtest — runs backtest AND appends one row to data/backtest_60d_log.jsonl
.\.venv\Scripts\python.exe nba_mod.py backtest_60d 2026-02-27
# Or default to yesterday automatically:
.\.venv\Scripts\python.exe nba_mod.py backtest_60d
# Optional overrides:
#   --window-days N      (default: 60)
#   --log-file <path>    (default: data/backtest_60d_log.jsonl)
#   --odds-db <path>     (default: standard OddsStore path)
```

## Output Files

- `data/backtest_results/<start>_to_<end>_full_local.json` for each window
- `data/backtest_60d_log.jsonl` — one-line JSON per 60d run (gitignored via `data/`)

## 60-Day Log Schema

Each line in `data/backtest_60d_log.jsonl`:

```json
{
  "runAt": "2026-02-28T14:30:00Z",
  "dateFrom": "2025-12-30",
  "dateTo": "2026-02-27",
  "windowDays": 60,
  "model": "full",
  "sampleCount": 33928,
  "realLineSamples": 8097,
  "missingLineSamples": 22631,
  "roiRealBets": 1986,
  "roiRealHitPct": 55.0,
  "roiRealPctPerBet": -0.16,
  "roiSimBets": 2757,
  "roiSimHitPct": 63.8,
  "roiSimPctPerBet": 21.7,
  "oddsSource": "local_history",
  "savedTo": "data/backtest_results/2025-12-30_to_2026-02-27_full_local.json"
}
```

Run `backtest_60d` weekly (e.g., every Sunday) to build a trend log over time.

## Compare Across Windows

| Metric | 7-day | 14-day | 30-day | 60-day |
|--------|-------|--------|--------|--------|
| realLineSamples | | | | |
| realLineHitRatePct | | | | |
| roiReal | | | | |
| Coverage % (real/total) | | | | |

## Interpretation

- **7-day:** Noisy; use for "is recent performance catastrophic?" not for GO/NO-GO.
- **14-day:** Sanity check; if 14d and 30d disagree sharply, investigate regime change.
- **30-day:** Primary decision metric; roiReal and hit rate here drive GO-LIVE.
- **60-day:** Trend/stability tracker only. Large roiReal swings vs 30d reveal variance or regime change. DO NOT use 60d as primary GO/NO-GO signal — 30d is the primary window.

---

## Model Version Summary (Include in Backtest Output)

Each saved backtest JSON should include a `modelVersion` (or `modelVersionSummary`) section describing how the model computes projections and EV. Update this when adding features so runs can be compared across versions.

**Example structure:**

```json
{
  "modelVersion": {
    "version": "v1",
    "projection": {
      "full": "defense (position-weighted), matchup history, position-vs-team, home/away, rest, pace factor",
      "simple": "defense only; no matchup or position-vs-team",
      "perMinRate": "60% weighted recent + 40% season rate",
      "statMultiplier": "defense * matchup, capped",
      "minutes": "base = weighted recent; adj = home/away, rest, trend; minutesMultiplier (streak, volatility, B2B)"
    },
    "ev": {
      "distribution": "Poisson for stl,blk,fg3m,tov; Normal for pts,reb,ast,pra",
      "stdev": "rolling stdev or 20% of projection",
      "calibration": "per-stat temperature scaling from prob_calibration.json",
      "edge": "model probOver vs no-vig implied"
    },
    "bettingPolicy": "statWhitelist, blockedProbBins, minEdgeThreshold"
  }
}
```

**When to update:** After adding no-vig, regression-to-mean, variance-by-sample-size, BRef integration, RAPM prior, or any other change to projection or EV logic. Bump `version` (e.g. v1 → v2) for major changes.
