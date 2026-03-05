# EV Engine Rules — Deep Dive

## Distribution Models

`compute_ev()` in `core/nba_ev_engine.py`:
- **Normal CDF:** pts, reb, ast, pra (continuous stats with moderate variance)
- **Poisson:** stl, blk, fg3m, tov (discrete, low-mean stats)
- Always pass `stat=` parameter for temperature-scaling calibration
- Edge computed vs **no-vig fair probability** (not raw book odds)

## Calibration Temperatures

Refitted 2026-03-01 (87 days, Dec 1–Feb 25, `--min-pred 0.01 --max-pred 0.25`):

**Global:** `pts=1.81 reb=3.79 ast=2.24 fg3m=1.49 pra=1.77 stl=1.39 blk=1.30 tov=1.25 _global=1.77`

**Per-bin overrides:**
| Stat | Bin 0 (0-10%) | Bin 1 (10-20%) |
|------|---------------|----------------|
| pts | 1.32 | 2.71 |
| ast | 1.76 | 3.21 |
| reb | 1.00 | 3.79 |
| pra | 1.38 | 2.45 |
| blk | 1.44 | 1.14 |

File: `models/prob_calibration.json` — `_fitted_at` must be < 60 days old.

## BETTING_POLICY (`core/nba_data_collection.py`)

- `stat_whitelist`: `{pts, ast}` — only these count for GO-LIVE gate
- `blocked_prob_bins`: `{1,2,3,4,5,6,7,8}` — bins 1+8 added 2026-03-03
- **Active betting bins:** 0 (0-10%, UNDER) + 9 (90-100%, OVER) only
- `no_blend=True` default in `compute_prop_ev()` since 2026-03-03

Removals: reb removed 2026-02-28 (-5.34% ROI), pra removed 2026-03-01 (-3.81% ROI).

## SIGNAL_SPEC (`core/nba_decision_journal.py`)

- `eligible_stats`: `{pts, reb, ast}` — signals that qualify for journal logging
- `min_edge`: 0.08 (per-stat: `{reb: 0.08, ast: 0.09}`)
- `min_confidence`: 0.60 (raised 2026-03-01 from 0.55)
- `real_line_required_stats`: `{reb}`

## Two-Layer Architecture

`gate_check()` in `core/gates.py` returns three ledgers:
1. **metrics** — BETTING_POLICY-qualified only (pts+ast), drives GO-LIVE gate
2. **model_leans** — all SIGNAL_SPEC signals, shows raw model predictive ability
3. **research_stats** — signals eligible but NOT in whitelist (reb), tracked separately
4. **edge_at_emission** — avg/min/max edge at pick time

## Edge Verdicts

| Edge | Verdict |
|------|---------|
| < 0 | Negative EV |
| < 0.08 | Thin Edge |
| 0.08–0.12 | Good Value |
| >= 0.12 | Strong Value |

## CLV Rule

`clvLine > 0` AND `clvOddsPct > 0` required for high-quality bets. Positive model EV alone is not sufficient.

## Pre-GO Calibration Checklist

- [ ] Avg Brier < 0.235 (baseline: 0.2325)
- [ ] All 8 stats show Brier improvement vs uncalibrated
- [x] Minutes 35+ bucket bias < +/-3.0 min (fixed: +6.24 -> +2.39 min)
- [ ] `models/prob_calibration.json` exists; `_fitted_at` < 60 days old
- [ ] blk/fg3m 60-80% bin gaps documented; NOT used as GO signals
- [ ] reb 60-70% bin gap < 12% (currently 9.2% post-cal)

## Poisson Accuracy Trap

fg3m/blk show high accuracy (96%/88%) but this is a **Poisson distribution shape artifact**, not model skill. Low-mean Poisson naturally clusters near zero = unders almost always hit. Meaningless without ROI-after-juice. Do not act on >60% confidence until recalibrated.

## Accuracy Baselines

| Period | Bets | Hit% | ROI | Notes |
|--------|------|------|-----|-------|
| In-sample (Dec 28–Feb 25) | 250 | 86.0% | +58.63% | Upper bound — all params tuned on this period |
| OOS (Oct 21–Nov 30, cal) | 228 | 71.1% | +31.86% | Cal temps contaminate bin assignment |
| OOS (Oct 21–Nov 30, no cal) | 488 | 67.4% | +22.48% | Edge survives without temp fitting |
| **Forward estimate** | — | — | **+20-30%** | Anchored on OOS bin-0 performance |
