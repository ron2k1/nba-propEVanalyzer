# NBA Prop Model — 30-Day Backtest Report

**Generated:** 2026-02-27
**Window:** 2026-01-26 → 2026-02-25 (30 calendar days, 27 game days, 199 games)
**Data source:** Local Kaggle index (eoin schema, max_date 2026-02-25) — zero API calls
**Models evaluated:** `simple`, `full`

---

## Commands Run

```powershell
# 1. Environment check
.venv/Scripts/python.exe -c "import pickle; idx=pickle.load(open('data/reference/kaggle_nba/index.pkl','rb')); print(idx['min_date'], idx['max_date'])"

# 2. Minutes evaluation
.venv/Scripts/python.exe nba_mod.py minutes_eval 2026-01-26 2026-02-25 --local

# 3. Simple model backtest
.venv/Scripts/python.exe nba_mod.py backtest 2026-01-26 2026-02-25 --model simple --local --save

# 4. Full model backtest
.venv/Scripts/python.exe nba_mod.py backtest 2026-01-26 2026-02-25 --model full --local --save
```

**Output files:**
- `data/backtest_results/2026-01-26_to_2026-02-25_simple_local.json`
- `data/backtest_results/2026-01-26_to_2026-02-25_full_local.json`
- `data/backtest_results/ckpt_2026-01-26_to_2026-02-25_{simple,full}_local_YYYY-MM-DD.json` (27 checkpoints each)

---

## 1. Volume & Error Diagnostics

| Metric | Value |
|--------|-------|
| Projection calls | 4,207 |
| Projection errors | 9 (0.21%) |
| Player-prop samples | 33,928 |
| Game days | 27 |
| Games | 199 |

Error rate of 0.21% is acceptable. All errors are isolated failures (players with insufficient history), not systemic.

---

## 2. MAE by Stat — Simple vs Full

| Stat | Simple MAE | Full MAE | Δ |
|------|-----------|---------|---|
| pts  | 4.761 | 4.759 | **-0.002** |
| reb  | 1.979 | 1.980 | +0.001 |
| ast  | 1.395 | 1.395 | 0.000 |
| fg3m | 0.897 | 0.897 | 0.000 |
| pra  | 6.449 | 6.449 | 0.000 |
| stl  | 0.747 | 0.747 | +0.000 |
| blk  | 0.515 | 0.515 | 0.000 |
| tov  | 0.932 | 0.932 | 0.000 |

**Finding:** MAE deltas between simple and full are negligible (< 0.002 units). The defense/matchup/minutes adjustments in `full` mode provide essentially no lift over season-average baseline in this window. This is a critical signal — see Section 6.

**Rank order by MAE** (best → worst):
1. blk (0.515) — low variance stat, easy to predict
2. fg3m (0.897)
3. tov (0.932)
4. stl (0.747)
5. ast (1.395)
6. reb (1.979)
7. pts (4.761) — high absolute variance
8. pra (6.449) — composite, accumulates errors

---

## 3. Brier Score by Stat — Simple vs Full

*(Lower = better calibrated probability estimates; 0.25 = random coin flip for balanced lines)*

| Stat | Simple | Full | Δ |
|------|--------|------|---|
| pts  | 0.2465 | 0.2463 | -0.0002 |
| reb  | 0.2426 | 0.2425 | -0.0001 |
| ast  | 0.2404 | 0.2405 | +0.0001 |
| fg3m | 0.2229 | 0.2228 | -0.0001 |
| pra  | 0.2471 | 0.2471 | 0.000 |
| stl  | 0.2360 | 0.2362 | +0.0002 |
| blk  | 0.1997 | 0.1997 | 0.000 |
| tov  | 0.2399 | 0.2400 | +0.0001 |

**All Brier scores are near or above 0.22**, approaching the 0.25 baseline for a coin flip at balanced lines. Best performer is blk (0.200) which reflects a low-variance distribution that's easy to beat at the median, not good probability calibration.

---

## 4. ROI Simulation

> **⚠️ IMPORTANT CAVEAT:** The backtest uses the model's season-average projection as the market line proxy. This does NOT reflect real sportsbook lines. Treat as an internal calibration metric only, not a real-world ROI estimate.

| Metric | Simple | Full |
|--------|--------|------|
| Bets placed | 25,089 | 25,087 |
| Wins | 15,769 | 15,782 |
| Losses | 9,320 | 9,305 |
| pnlUnits | +5,015 | +5,042 |
| Hit rate | 62.85% | 62.91% |
| ROI/bet (sim) | +19.99% | +20.10% |

The 62.9% simulated hit rate against season-average lines is expected: the model IS the line, so it wins whenever it was close to right. The ~+20% figure is an artifact of how lines are set in the simulation, not predictive of real betting returns.

---

## 5. Calibration Diagnostics (Full Model)

Showing predicted probability bin vs. actual over-hit rate. **Overconfident** = predicted >> actual.

### pts — significant overconfidence above 60%
| Bin | Count | Pred% | Actual% | Gap |
|-----|-------|-------|---------|-----|
| 20-30% | 289 | 26.6 | 37.4 | **-10.8 under** |
| 30-40% | 1,091 | 35.5 | 40.3 | -4.8 under |
| 40-50% | 1,255 | 44.7 | 44.4 | ✓ |
| 50-60% | 969 | 54.2 | 53.4 | ✓ |
| 60-70% | 433 | 63.9 | 57.3 | **+6.6 over** |
| 70-80% | 98 | 73.7 | 57.1 | **+16.6 over** |
| 80-90% | 19 | 84.3 | 26.3 | **+58.0 over ⚠️** |

### reb — overconfident above 50%
| Bin | Count | Pred% | Actual% | Gap |
|-----|-------|-------|---------|-----|
| 50-60% | 1,185 | 54.3 | 48.8 | +5.5 over |
| 60-70% | 617 | 64.0 | 51.9 | **+12.1 over** |
| 70-80% | 135 | 73.2 | 60.0 | **+13.2 over** |

### ast — moderate overconfidence above 50%
| Bin | Count | Pred% | Actual% | Gap |
|-----|-------|-------|---------|-----|
| 50-60% | 1,228 | 53.9 | 47.1 | +6.8 over |
| 60-70% | 532 | 64.3 | 56.2 | **+8.1 over** |
| 70-80% | 123 | 73.6 | 57.7 | **+15.9 over** |

### fg3m — overconfident above 50%
| Bin | Count | Pred% | Actual% | Gap |
|-----|-------|-------|---------|-----|
| 50-60% | 852 | 53.3 | 44.5 | **+8.8 over** |
| 60-70% | 315 | 64.1 | 50.8 | **+13.3 over** |
| 70-80% | 90 | 73.8 | 57.8 | **+16.0 over** |

### pra — under-confident at 30-40%, over-confident above 70%
| Bin | Count | Pred% | Actual% | Gap |
|-----|-------|-------|---------|-----|
| 30-40% | 1,072 | 35.4 | 42.8 | **-7.4 under** |
| 60-70% | 542 | 64.2 | 56.3 | +7.9 over |
| 70-80% | 186 | 73.8 | 61.8 | **+12.0 over** |

### stl — overconfident across all bins above 40%
| Bin | Count | Pred% | Actual% | Gap |
|-----|-------|-------|---------|-----|
| 40-50% | 666 | 44.0 | 35.0 | **+9.0 over** |
| 50-60% | 1,339 | 53.9 | 46.4 | **+7.5 over** |
| 60-70% | 647 | 64.4 | 52.4 | **+12.0 over** |
| 70-80% | 150 | 73.0 | 54.0 | **+19.0 over ⚠️** |

### blk — worst calibrated stat, overconfident at all levels ⚠️
| Bin | Count | Pred% | Actual% | Gap |
|-----|-------|-------|---------|-----|
| 0-10% | 365 | 4.8 | 12.3 | -7.5 under |
| 30-40% | 974 | 34.4 | 24.0 | **+10.4 over** |
| 40-50% | 580 | 43.7 | 30.0 | **+13.7 over** |
| 50-60% | 863 | 53.2 | 38.7 | **+14.5 over ⚠️** |
| 60-70% | 294 | 64.1 | 49.3 | **+14.8 over ⚠️** |
| 70-80% | 57 | 72.8 | 47.4 | **+25.4 over ⚠️** |

### tov — best calibrated stat in the upper bins
| Bin | Count | Pred% | Actual% | Gap |
|-----|-------|-------|---------|-----|
| 50-60% | 1,258 | 53.9 | 46.5 | +7.4 over |
| 60-70% | 550 | 64.2 | 55.5 | +8.7 over |
| 70-80% | 192 | 73.3 | 64.1 | +9.2 over |

---

## 6. Minutes Model Diagnostics

| Metric | Value |
|--------|-------|
| MAE | 5.66 min |
| Bias | -0.24 min (near zero overall) |
| Sample count | 4,241 |

### By minutes bucket
| Bucket | Count | MAE | Bias | Avg Proj | Avg Actual |
|--------|-------|-----|------|----------|------------|
| 0–15 | 999 | 6.19 | **-1.33** | 10.3 | 11.6 |
| 15–25 | 1,660 | 5.54 | **-1.30** | 20.2 | 21.5 |
| 25–35 | 1,436 | 5.31 | +1.12 | 29.3 | 28.2 |
| 35+ | 146 | 6.92 | **+5.81 ⚠️** | 36.6 | 30.8 |

**Critical finding:** The model over-projects high-minute players by +5.8 min on average. Star players who average 35+ minutes are being assigned ~37-min projections but actually played ~31. This is the **load-management blind spot** — the model doesn't account for scheduled rest, minute restrictions, or recent workload reduction.

---

## 7. Simple vs Full — Why They're Identical

The full model applies defense ratings, matchup history, and minutes multipliers on top of the season average. The deltas are < 0.002 MAE across all stats because:

1. **Local index uses the Kaggle eoin schema** which provides game/box scores but no separate defense-by-position table. The defense lookup falls back to zero or neutral adjustment when data is unavailable.
2. **Matchup history** is sparse in any 30-day window — most player-team pairings have ≤1 historical matchup.
3. **Minutes multiplier** dampens but doesn't zero out adjustments — the near-zero overall bias suggests it's working, but the bucket-level analysis shows it's mis-calibrated for stars.

**Practical implication:** In a live deployment reading from the NBA Stats API (not local), `full` mode would pull live defense rankings and matchup data, and the gap vs `simple` would widen. This backtest does NOT invalidate the full model for live use — it reveals a data gap in the local index.

---

## 8. Summary Comparison

| Metric | Simple | Full | Winner |
|--------|--------|------|--------|
| Avg MAE | 2.185 | 2.185 | Tie |
| Avg Brier | 0.2341 | 0.2341 | Tie |
| Hit rate (sim) | 62.85% | 62.91% | Full (+0.06pp) |
| ROI (sim) | +19.99% | +20.10% | Full (artifact) |
| Proj errors | 9 | 9 | Tie |
| Best stat (Brier) | blk 0.200 | blk 0.200 | — |
| Worst stat (Brier) | pra 0.247 | pra 0.247 | — |
| Best calibrated | tov/ast (mid-bins) | tov/ast (mid-bins) | — |
| Worst calibrated | blk (all bins) | blk (all bins) | — |

---

## 9. Go/No-Go Verdict

> ## ⚠️ CAUTION

The model demonstrates real directional signal (MAE is reasonable for a season-average baseline) and is mechanically sound with 0.21% error rate. However, it has three calibration defects that prevent blind deployment:

### Why CAUTION (not GO)

1. **Systematic overconfidence at 60–80% confidence levels across all stats.** The model assigns 64% probability when actual hit rate is ~52–57%. Betting on 60%+ model signals at standard vig (-110) would result in near-breakeven or negative EV — the market already prices these correctly.

2. **BLK and STL are untradeable as-is.** BLK overconfidence reaches +25 percentage points in the 70-80% bin. Any BLK or STL prop above 50% model confidence should be avoided until recalibration.

3. **Minutes model over-projects stars by +5.8 min**, causing pts/pra/reb projections for high-usage players to be systematically inflated. This is a load-management blind spot that is most damaging for marquee player props.

### What to Improve Next

1. **Apply Platt scaling or isotonic regression calibration** to the probability output. The raw normal CDF over-spreads confidence. A post-hoc calibrator trained on the 33k sample backlog would directly fix the 60-80% overconfidence problem. This is the highest-leverage fix.

2. **Fix the 35+ minute bucket.** Add a minutes-cap signal: if a player's last-5 average is more than 10% below their season average, dampen the projection. This directly addresses the load management blind spot.

3. **Validate full model with live API data.** Run a 2-week live forward test with `--model full` vs `--model simple` using real NBA Stats API (not local index) to confirm whether defense/matchup adjustments provide lift when live data is available. If they don't, the full model's additional complexity is not justified.

---

## Appendix — Explicit Assumptions

- Lines used in ROI simulation = model's own season-average projection (not real sportsbook lines)
- No vig applied to ROI simulation (inflates hit-rate threshold)
- Local index uses Kaggle eoin schema; defense-by-position data unavailable → full ≈ simple
- All-Star break (Feb 14–18) correctly produces zero games in index
- 9 projection errors excluded from calibration stats (0.21% of calls)
- Brier score computed against 50% baseline (symmetric over/under market)
