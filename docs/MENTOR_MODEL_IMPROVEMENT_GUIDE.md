# Step-by-Step: Fix the Model Math & Backtest Against Real Closing Odds

**Mentor mode. No sugarcoating. Facts only.**

---

## The Uncomfortable Truth First

1. **Breakeven at -110 is 52.38%.** You need to beat that. 55% → ~5% ROI. 56% → ~8%. World-class sharp books are ~58–60% on player props. Your 56% real-line hit rate is survivable but not special.

2. **Synthetic ROI (+20–24%) is fake.** It grades your bets against a line you invented (floor(projection)+0.5). The market never offered that. The moment you backtest against real closing lines, ROI collapses. **Never use synthetic for any decision.**

3. **24% real-line coverage** means 76% of your backtest samples used synthetic. Your roiReal (~+1.56%) is from ~1,700 bets. That’s the only number that matters.

4. **If you don’t have real closing data, you cannot quantize the model.** Synthetic backtests only measure calibration, not edge.

---

## Step 1: Get Real Historical Closing Odds

### Option A: Odds API (You Already Have It)

Odds API **does** provide historical closing odds. Your project uses it:

- **Endpoint:** `historical/sports/basketball_nba/events/{event_id}/odds`
- **Params:** `date` = UTC timestamp (e.g. tipoff − 60 min for near-close)
- **Flow:** `odds_backfill` → OddsStore snapshots → `odds_build_closes` → closing_lines table

**Commands (you have 100k credits):**

```powershell
# Backfill in 7-day chunks. ~19.5k credits per week.
.\.venv\Scripts\python.exe nba_mod.py odds_backfill 2025-11-01 2025-11-07 --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra --offset-minutes 60 --max-requests 1950 --resume

# Rebuild closing lines after each chunk
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes 2025-11-01 2026-02-26

# Verify coverage
.\.venv\Scripts\python.exe nba_mod.py odds_coverage --by-date 2025-11-01 2026-02-26
```

**Limitation:** Odds API doesn’t return closing lines for every player. Coverage tops out around 20–30%. That’s a data-depth limit, not a backfill bug.

---

### Option B: Alternative Provider (If Odds API Is Insufficient)

If you need better coverage or a different provider:

| Provider          | Likely Has Closing | Adapter Needed |
|-------------------|--------------------|----------------|
| Prop Odds API     | Yes                | Yes — write loader into OddsStore snapshots |
| SportsGameOdds    | Yes                | Yes |
| RapidAPI historical | Unclear for NBA props | Verify before committing |
| Bulk CSV/Parquet  | If you have it     | Use `stage_local_parquet.py` pattern |

**Rule:** Whatever source you use must write into the OddsStore `snapshots` schema so `odds_build_closes` works unchanged. Don’t invent a parallel pipeline.

---

## Step 2: Run Real-Line-Only Backtest

**No synthetic. Ever.**

```powershell
# date_to MUST be before today (no lookahead)
.\.venv\Scripts\python.exe nba_mod.py backtest 2025-11-01 2026-02-25 --model full --local --odds-source local_history --real-only --save
```

- `--real-only` skips any sample without a real closing line. No fake fallback.
- Output: `data/backtest_results/<range>_full_local.json`

---

## Step 3: Quantize — What to Measure

From the backtest JSON, focus on these. Ignore roiSynth.

### Primary Metrics

| Metric            | What It Is                         | Target        |
|-------------------|------------------------------------|---------------|
| roiReal.roiPctPerBet | ROI on real-line bets only      | > 0% (ideally > 2%) |
| roiReal.hitRatePct   | Win rate on real-line bets      | > 52.38% (breakeven) |
| realLineSamples      | Count of real-line samples      | Higher = more reliable |
| roiReal.betsPlaced   | Bets graded with real lines    | ≥ 500 for stable estimates |

### Calibration (Model Confidence vs Reality)

| Metric              | What It Is                                    | Target   |
|---------------------|-----------------------------------------------|----------|
| brierByStat[stat]   | (pred_prob − actual)², lower = better         | < 0.24   |
| realLineCalibBins   | 10 bins: 0–10%, 10–20%, … 90–100% model prob | Each bin’s actual hit rate should track predicted rate |

**Bin check:** If the model says 65% and the bin hits at 52%, it’s overconfident. Fix calibration (temperature scaling) or projection.

### Projection Accuracy

| Metric            | What It Is                 | Target   |
|-------------------|----------------------------|----------|
| maeByStat[stat]   | Mean absolute error (line) | Lower is better |
| minutesBias       | Projected − actual minutes | < ±2 min |
| minutesMae        | MAE on minutes             | Lower is better |

---

## Step 4: Fix What’s Broken (In Order)

### 4.1 Calibration (Temperature Scaling)

Model is overconfident in some bins. Temperature T > 1 pulls probabilities toward 50%.

**Refit from backtest output:**

```powershell
.\.venv\Scripts\python.exe scripts\fit_calibration.py --input data/backtest_results/2025-11-01_to_2026-02-25_full_local.json --output models/prob_calibration.json
```

**Check:**  
`realLineCalibBins` — bins with big gaps (e.g. 65% predicted, 52% actual) are overconfident. Refit should shrink those.

---

### 4.2 Minutes Model (Over-Projecting Stars)

`minutesBias` on the 35+ minute bucket was +5.8 min before the fix. If it’s still high:

- File: `core/nba_minutes_model.py`
- Tune `_SOFT_CAP`, `_DECAY` for high-minute players
- Run `minutes_eval` to verify:

```powershell
.\.venv\Scripts\python.exe nba_mod.py minutes_eval 2025-11-01 2026-02-25 --local
```

---

### 4.3 Projection Math (Defense, Matchup, Variance)

Projection lives in `core/nba_prep_projection.py` and `core/nba_local_stats.py`.

**Metrics to watch:**
- MAE by stat — if one stat is much worse, inspect that path
- Per-stat ROI — if a stat is consistently negative (e.g. reb was -5.9%), remove it from the whitelist or fix the projection

**Do not:** Add RAPM, BRef splits, or fancy priors before you’ve fixed calibration and minutes. Those are marginal gains. Fix the core first.

---

### 4.4 EV Math (No-Vig, Distribution)

`core/nba_ev_engine.py`:
- Normal CDF for pts, reb, ast, pra
- Poisson for stl, blk, fg3m, tov
- Edge = model probOver vs no-vig implied (over_implied / (over+under))

If no-vig or distribution is wrong, EV and edge will be off. Check:
- `compute_ev()` always receives `stat=` for correct calibration
- No-vig: `american_to_implied_prob` applied to both sides, then normalize

---

## Step 5: Refit → Re-Backtest → Compare

1. Change one thing (e.g. calibration temps).
2. Re-run backtest with same date range, same `--real-only`.
3. Compare roiReal, hitRatePct, brierByStat, realLineCalibBins before vs after.

**Rule:** Same dates, same books, same odds DB. Only change the model. Otherwise you’re mixing variables.

---

## Step 6: If Odds API Doesn’t Have What You Need

**Questions to ask the provider:**
- Do you expose **historical** player prop odds (not only live)?
- Do you have **closing** lines (last snapshot before tip)?
- Which sportsbooks? (BetMGM, DraftKings, FanDuel preferred)
- Which stats? (pts, reb, ast, pra minimum)

**Integration:**
1. Write a loader that outputs rows compatible with OddsStore `snapshots`.
2. Insert into `data/reference/odds_history/odds_history.sqlite`.
3. Run `odds_build_closes` as usual.
4. Backtest with `--odds-source local_history --real-only`.

---

## Harsh Summary

1. **Real data first.** No real closing odds → no meaningful backtest.
2. **roiReal only.** Synthetic ROI is not a performance metric.
3. **52.38% is breakeven.** Model must beat that on real lines.
4. **Fix calibration before chasing projection gains.** Temperature scaling has the biggest impact for the least code.
5. **One change at a time.** Refit, backtest, compare. Don’t mix multiple changes.
6. **Drop stats that lose.** reb was -5.9%; you removed it. Good.
7. **Coverage ~24% is a limit.** Odds API doesn’t cover every player. More data may require another provider.
8. **No AI bias here.** The math is standard: Brier, calibration bins, MAE, ROI. Use them.

---

## Quick Reference: Full Pipeline

```powershell
# 1. Backfill (if needed)
nba_mod.py odds_backfill 2025-11-01 2025-11-07 --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra --offset-minutes 60 --max-requests 1950 --resume

# 2. Build closes
nba_mod.py odds_build_closes 2025-11-01 2026-02-26

# 3. Check coverage
nba_mod.py odds_coverage --by-date 2025-11-01 2026-02-26

# 4. Backtest (real-only)
nba_mod.py backtest 2025-11-01 2026-02-25 --model full --local --odds-source local_history --real-only --save

# 5. Refit calibration
scripts\fit_calibration.py --input data/backtest_results/<file>.json --output models/prob_calibration.json

# 6. Minutes eval (optional)
nba_mod.py minutes_eval 2025-11-01 2026-02-25 --local
```
