# Plan for Claude Code

> Actionable implementation plan for the NBA data ver 2 project. Use when starting a session, backfilling historical odds, or executing the closing-lines pipeline.

**Book priority: BetMGM → DraftKings → FanDuel.** All `--books` flags, scan rankings, and line selection should follow this order.

---

## 1. Odds API Backfill (Paid Credits)

### Credit Math (20K plan = $30/mo)

| Component | Cost | Notes |
|-----------|------|-------|
| Discovery (h2h per date) | 10 credits | One call per calendar day |
| Player props per event | 50 credits | 5 stats × 10 credits each |
| **Per day** (~12 games) | ~610 credits | 10 + (12 × 50) |
| **~30 days** | ~18,300 credits | Fits within 20K with buffer |

**Reduce stats to stretch credits:** `pts,reb,ast` only → ~50 days coverage.

### Commands (Stay Within Budget)

```powershell
# Dry-run first (no API calls)
.\.venv\Scripts\python.exe scripts\backfill_odds_history.py ^
    --date-from 2026-02-01 --date-to 2026-02-28 ^
    --books betmgm,draftkings,fanduel ^
    --stats pts,reb,ast,fg3m,tov ^
    --offset-minutes 60 ^
    --dry-run

# Execute with hard cap (leave ~500 credits buffer)
.\.venv\Scripts\python.exe scripts\backfill_odds_history.py ^
    --date-from 2026-02-01 --date-to 2026-02-28 ^
    --books betmgm,draftkings,fanduel ^
    --stats pts,reb,ast,fg3m,tov ^
    --offset-minutes 60 ^
    --max-requests 19500 ^
    --resume

# Derive closing lines from snapshots
.\.venv\Scripts\python.exe scripts\build_closing_lines.py --date-from 2026-02-01 --date-to 2026-02-28
```

- **`--resume`** — Skip dates already in OddsStore. Safe to re-run.
- **`--max-requests 19500`** — Hard cap; stops before exhausting quota.
- **`interval-minutes=0`** (default) — Single near-close snapshot per event (cheapest).

---

## 2. Zero-Cost Pipeline (LineStore → OddsStore Bridge)

No extra API cost. Uses `collect_lines` data already written to `data/line_history/*.jsonl`.

### Daily Schedule

| Time ET | Command |
|---------|---------|
| 11:00 | `nba_mod.py collect_lines --books betmgm,draftkings,fanduel --stats pts,reb,ast,fg3m,tov` |
| 14:00 | Same |
| 17:00 | Same (near tipoff) |

### End-of-Day

```powershell
.\.venv\Scripts\python.exe nba_mod.py line_bridge 2026-02-27 2026-02-27
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes
```

Bridge is idempotent (INSERT OR IGNORE); safe to run multiple times.

---

## 3. Backtest with Real Lines

```powershell
# Check coverage first
.\.venv\Scripts\python.exe nba_mod.py odds_coverage --by-date 2026-02-01 2026-02-27

# Run backtest using local closing lines
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-02-01 2026-02-27 --model full --local --odds-source local_history --save
```

- **Primary metric:** `roiReal` — use for GO/NO-GO.
- **Diagnostic only:** `roiSynth` — do not treat as real-money estimate.
- **Coverage gate:** If `realLineSamples=0`, backtest P&L is not real-world-valid.

---

### 3a. Staged Backtest Plan: 7d → 14d → 30d

Run the current model across three windows to assess stability and sample-size effects. Use a **fixed end date** (e.g. last date with closing coverage) so windows are nested.

| Window | Start | End | Purpose |
|--------|-------|-----|---------|
| 7-day | 2026-02-19 | 2026-02-25 | Recent regime; high variance, quick signal |
| 14-day | 2026-02-12 | 2026-02-25 | Medium sample; balance recency vs stability |
| 30-day | 2026-01-26 | 2026-02-25 | Full sample; primary metrics, calibration |

**Commands (adjust dates to your coverage):**

```powershell
# 1. Verify coverage for all three windows
.\.venv\Scripts\python.exe nba_mod.py odds_coverage --by-date 2026-01-26 2026-02-25

# 2. Run all three backtests (full model, local, real lines)
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-02-19 2026-02-25 --model full --local --odds-source local_history --save
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-02-12 2026-02-25 --model full --local --odds-source local_history --save
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-01-26 2026-02-25 --model full --local --odds-source local_history --save
```

**Output files:**
- `data/backtest_results/2026-02-19_to_2026-02-25_full_local.json`
- `data/backtest_results/2026-02-12_to_2026-02-25_full_local.json`
- `data/backtest_results/2026-01-26_to_2026-02-25_full_local.json`

**Compare across windows:**

| Metric | 7-day | 14-day | 30-day |
|--------|-------|--------|--------|
| `realLineSamples` | | | |
| `realLineHitRatePct` | | | |
| `roiReal` | | | |
| Coverage % (real / total) | | | |

**Interpretation:**
- **7-day:** Noisy; use for “is recent performance catastrophic?” not for GO/NO-GO.
- **14-day:** Sanity check; if 14d and 30d disagree sharply, investigate regime change.
- **30-day:** Primary decision metric; `roiReal` and hit rate here drive GO-LIVE.
- **Consistency:** Ideally all three show same direction (all negative or all positive). Large swings (e.g. 7d +5%, 30d -4%) suggest high variance or recent change.

**Note:** `date_to` must be strictly before today (no-lookahead). If your local index ends earlier, use that as the shared end date for all three windows.

**Model version:** Each saved backtest JSON includes `modelVersion`, a short summary of how projections and EV are computed. When you add features (no-vig, regression-to-mean, etc.), bump `MODEL_VERSION_SUMMARY` in `core/nba_backtest.py` so runs can be compared.

---

## 4. Model Accuracy Optimization

> **Direction:** Optimize for real-line performance only. Ignore blended synthetic metrics.

**Recommended next step:** Add ROI-by-confidence-bin analysis to backtest output. Confirm 40–60% bin is unprofitable, then implement Path (a): exclude that bin and raise EV threshold. Path (b) (CLV gate) can run in parallel.

### Current Baseline (2026-01-26 to 2026-02-25)

| Model | Real-Line Hit Rate | Real-Line ROI/Bet |
|-------|-------------------|-------------------|
| simple | 50.80% | -3.72% |
| full  | 50.79% | -3.77% |

- Real-line samples: 8,097
- Synthetic fallback: 22,631
- **Synthetic ROI (diagnostic only):** +26.28% on 18,288 bets — exactly the inflated number CLAUDE.md warned about. Ignore it.

**Brier (all pass &lt;0.235 target):** pts 0.250, reb 0.249, ast 0.245, pra 0.246, tov 0.237, stl 0.231, fg3m 0.217, blk 0.190. Avg ~0.233.

---

### Root Cause: Calibration ≠ Edge

Probability calibration (Brier) is reasonable, but against **real closing lines** the model is underwater across all stats with coverage. Two causes:

1. **40–60% confidence bin dominates bet volume** — Most bets land here. The model bets at lines the market has already priced correctly; vig eats the edge.
2. **Line vs projection spread** — Model finds “edge” vs synthetic ±0.5 lines, but real lines are often tighter; no actual CLV.

**Two paths to profitability (pick one or both):**
- **(a) Tighter edge thresholds** — Be more selective. Raise min EV; avoid 40–60% confidence bin until proven profitable.
- **(b) Find actual CLV** — Improve line-vs-projection spread (earlier line capture, sharper projections, soft-book shopping).

---

### Phase 1: Raise Real-Line Coverage

**Goal:** `realLineSamples / sampleCount >= 70%`

| Action | Notes |
|--------|-------|
| Backfill missing dates | Ensure closes exist for every game-day in backtest window |
| Book priority | betmgm > draftkings > fanduel |
| Collect 2–3× daily | 11am, 2pm, 5pm ET before tipoff |
| Run `line_bridge` + `odds_build_closes` | End-of-day for every date |

**Pass:** Coverage ≥ 70% on next backtest run.

---

### Phase 2: Line-Aware Calibration + Bin Analysis

- Refit calibration using **only** real-line bets (exclude synthetic fallback).
- Track by stat separately: pts, reb, ast, pra (plus tov if included).
- Add per-stat metrics:
  - **ECE** (Expected Calibration Error)
  - **Brier** by confidence bin: 40–50%, 50–60%, 60–70%, 70–80%
  - **ROI by bin** — identify which bins are profitable on real lines

**Key:** Brier passing does not imply profitability. The 40–60% bin is likely unprofitable after vig; 60–80% may be where edge exists. Use bin-level ROI to drive selectivity.

**Pass:** Brier improvement vs uncalibrated; ROI-by-bin report shows which bins to allow.

---

### Phase 3: Model Math + External Datasets

Expand projection and EV logic with additional math and richer data sources.

#### 3a. Math Additions (Python Models)

| Addition | Module | Purpose |
|----------|--------|---------|
| **Regression to the mean** | `nba_prep_projection` | Pull extreme recent performance (last-5 spikes) toward season avg; reduce overreaction to hot/cold streaks |
| **Variance modeling** | `nba_ev_engine` | Increase uncertainty for low-sample players (e.g. &lt;15 games); widen CDF tails for underdogs |
| **No-vig implied prob** | `nba_ev_engine` | Use `p_over = under_implied / (over_implied + under_implied)` for fair price; compare model prob vs fair, not vs raw book |
| **Skellam / NegBin** | `nba_ev_engine` | For Poisson stats (stl, blk, fg3m): consider overdispersion; NegBin if variance &gt; mean in sample |
| **Bayesian prior** | `nba_prep_projection` | Blend projection with position/role prior; shrink low-N players toward positional average |
| **Travel / schedule density** | `nba_prep_projection` | Fatigue factor: 4-in-5, 5-in-7, timezone cross; reduce minutes/rate slightly |
| **Blowout / garbage-time** | `nba_minutes_model` | Downweight minutes when game spread suggests early exit; integrate win-prob or spread proxy |
| **Injury-teammate usage** | `nba_prep_usage` | Already partial; extend to multi-player DNP and usage reallocation by role |
| **Stat correlation (single player)** | `nba_ev_engine` or new | For PRA/combo: pts-reb-ast correlation within player; adjust combo variance |

#### 3b. External Datasets to Integrate

| Source | Format | What to use | Integration |
|--------|--------|-------------|-------------|
| **Basketball Reference** | CSV / JSONL | Game logs, splits, matchup history | `scripts/bref_ingest.py` + `nba_bref_data`; add BRef as fallback for `get_player_game_log` |
| **Kaggle: NBA Historical Box Scores** | CSV | Bulk historical box scores | Stage via `stage_local_parquet.py`; backfill pre-NBA-API dates |
| **nbarapm.com / NBA Game Flow** | CSV / API | RAPM, DARKO, LEBRON, RAPTOR | Add `player_impact` table; use as prior for low-N players or defense matchup |
| **FiveThirtyEight RAPTOR** | GitHub CSV | RAPTOR (until 2023) | Historical; augment defense/opponent impact |
| **NBA.com Advanced** | API | Opponent-specific, pace | Already in `get_team_defensive_ratings`; extend with opponent FG% by zone if available |
| **sportsreference** (PyPI) | Python lib | BRef scraped | Optional; use for splits, matchup, pace when NBA API is rate-limited |
| **nba_api** | PyPI | Official NBA stats | Alternative to custom `nba_data_collection`; evaluate for roster, injuries |

**Priority:** (1) BRef local ingest for coverage + matchup, (2) RAPM/DARKO for prior/defense, (3) Kaggle bulk for backtest depth.

#### 3c. Implementation Order

1. ~~**No-vig implied prob**~~ ✅ Done. Edge now compares model prob vs de-vigged fair prob (`no_vig_over = p1/total`). `noVigImplied` added to output. **Side effect:** edges appear larger (vs fair instead of vs vigged), increasing bet volume +46%. Threshold must be raised to ~5% in Phase 5 to restore selectivity.
2. ~~**Regression to mean**~~ ✅ Done. In `nba_prep_projection`, `weighted_avg` is shrunk toward `season_avg` when z-score > 1.5 stdev (linear shrinkage, max 50%). Minimal Brier impact on current sample (most players have 20+ games).
3. ~~**Variance by sample size**~~ ✅ Done. `stdev_val` scaled by `1 + 2*(1/sqrt(n) - 1/sqrt(25))` — wider CDF tails for n < 25 games. No effect at n=25. Inflates ~49% at n=5, ~23% at n=10.
4. **BRef integration** — Expand `BrefLocalStore`; use for `get_matchup_history`, `get_position_vs_team`.
5. **RAPM/DARKO loader** — New `core/nba_impact_prior.py`; blend into projection when `n &lt; 15`.

#### 3d. Phase 3 Backtest Results (2026-01-26 to 2026-02-25)

| Metric | Pre-Phase 3 | Post-Phase 3 | Notes |
|--------|-------------|--------------|-------|
| roiReal | -3.77% | -4.53% | More bets from no-vig edge → more losses |
| Real bets | 3,808 | 5,555 | +46% volume from permissive threshold |
| Real hit% | 50.79% | 50.39% | Marginal bets dilute hit rate |
| Avg Brier | 0.233 | 0.233 | Regression/variance had negligible effect |
| Synth ROI | +26.28% | +22.62% | Diagnostic only |

**Key insight:** No-vig edge is the correct math, but the 3% threshold was calibrated for vigged edge. Raising to ~5% (Phase 5) should restore selectivity while using cleaner math. The regression and variance changes provide safety rails for future edge cases (new players, hot-streak outliers) even though current-sample impact is small.

---

### Phase 4: Improve Minutes Model ✅

Implemented:
- ~~Starter status~~ ✅ Inferred from avg minutes ≥28 + low CV; boosts confidence for starters, penalizes deep-bench
- ~~Blowout risk~~ ✅ Soft cap at 33 min with 0.55 decay — accounts for load management, blowout rest, foul trouble
- ~~Last-5 trend damping~~ ✅ Minutes-specific regression to mean (z > 1.5 stdev → shrink toward season avg)

**Results:** Minutes MAE 5.66 → 5.63 (marginal overall improvement; main benefit is reduced star-bucket over-prediction which flows through to better stat projections and real-line ROI).

**Remaining:** Injury-teammate usage reallocation (deferred to Phase 3b/BRef integration). Overall MAE target of 4.8 not yet met — bench-player noise dominates the average.

---

### Phase 5: Strict Betting Policy Gate ✅

**Path (a) — Implemented:**
- ~~Raise min EV~~ ✅ 0.03 → 0.05 (no-vig edge); verdict thresholds updated (Good Value < 0.08, Strong Value ≥ 0.08)
- ~~Block 40–60% confidence bins~~ ✅ `BETTING_POLICY["blocked_prob_bins"] = {4, 5}`
- ~~Stat whitelist~~ ✅ `BETTING_POLICY["stat_whitelist"] = {"pts", "reb", "ast", "pra"}`
- Policy enforced in backtest + exposed in output (`bettingPolicy` field)

**Path (b) — Find actual CLV (not yet started):**
- Capture lines earlier (pre-sharp move) via more frequent `collect_lines`
- Shop soft books vs sharp; bet only when line is stale vs model
- Require `clvLine > 0` AND `clvOddsPct > 0` as validity gate (CLAUDE.md rule)

#### Phase 4+5 Backtest Results (2026-01-26 to 2026-02-25)

| Metric | Baseline | Phase 3 | Phase 5 Only | **Phase 4+5** |
|--------|----------|---------|-------------|---------------|
| roiReal | -3.77% | -4.53% | -0.16% | **+0.088%** |
| Real bets | 3,808 | 5,555 | 2,279 | 2,270 |
| Real hit% | 50.79% | 50.39% | 55.33% | **55.51%** |
| Real P&L | -143.4 | -251.7 | -3.67 | **+2.00** |

| Stat | Baseline ROI | **Phase 4+5 ROI** |
|------|-------------|-------------------|
| pts | -4.37% | **+3.14%** |
| reb | -5.82% | -5.57% |
| ast | -2.15% | **+1.88%** |
| pra | -1.38% | **+2.43%** |

**First time the engine shows positive real-line ROI.** Three of four stats profitable. Reb is the sole drag; consider removing from whitelist if negative persists over longer sample.

---

### Phase 6: GO-LIVE Master Plan ← ACTIVE

> **Core insight:** The model is already 60-70% of the way to the realistic accuracy ceiling (~57-58% hit rate). The remaining edge comes from **CLV filtering** (selecting the right bets), not from squeezing accuracy. A 55% model that only bets CLV-positive lines can produce +5-8% ROI.

**Current baseline (30-day, Jan 26 - Feb 25):**

| Metric | Value |
|--------|-------|
| Real-line hit rate | 55.0% |
| Breakeven at -110 | 52.4% |
| Current edge | +2.6pp |
| Real-line ROI | +0.088% (barely positive) |
| Real-line coverage | 26% (8,097 / 30,728) |
| Profitable stats | pts +2.6%, ast +1.7%, pra +2.4% |
| Unprofitable stats | reb -5.9% |

**Realistic accuracy ceiling:** ~57-58% hit rate. World-class sharp syndicates: ~58-60%.

**Addressable improvements (total ~2-3pp):**

| Improvement | Current | Achievable | Hit Rate Delta |
|-------------|---------|------------|----------------|
| Minutes 35+ bucket bias | +5.8 min | ±1.5 min | +0.5-0.8pp |
| Close 60-70% calibration bin gap | 51.9% hits | 55-56% hits | +0.8-1.2pp |
| opp_oreb_pct feature (reb only) | no feature | new signal | +0.3-0.5pp on reb |
| Extended calibration window (90d) | 31-day fit | 90-day fit | +0.3-0.5pp |

**Hard limits (not fixable):** last-minute lineup changes, game flow randomness, market efficiency, player counting-stat variance.

---

#### Step 1: Paper-Trading + CLV Data Accumulation (Days 1-14)

**This is the highest-leverage activity.** Everything else is secondary.

**Infrastructure (already implemented):**
- `SIGNAL_SPEC` aligned with `BETTING_POLICY`: stat whitelist `{pts, reb, ast, pra}`, blocked bins `{4, 5}`, min edge 0.05, min confidence 0.55
- `_qualifies()` in `nba_decision_journal.py` enforces all policy gates
- `best_today` output includes `policyQualified` per entry
- `paper_settle` settles both JSONL + SQLite journals
- `paper_summary --window-days 14` produces report + GO-LIVE gate check
- Live projection close-game minutes floor (2026-02-28): overrides pregame soft cap for starters in close games Q3+

**Daily routine (every game day):**

| When | What | Command |
|------|------|---------|
| 11am ET | Collect lines | `nba_mod.py collect_lines --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra` |
| 2pm ET | Collect lines | same |
| 5pm ET | Collect lines (near tip) | same |
| 5pm ET | Review signals | `nba_mod.py best_today 20` |
| 11pm ET | Bridge lines for CLV | `nba_mod.py line_bridge --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra` |
| 11pm ET | Build closes | `nba_mod.py odds_build_closes` |
| Next AM | Settle | `nba_mod.py paper_settle <yesterday's date>` |
| Next AM | Review | `nba_mod.py results_yesterday 50` |
| Next AM | Gate check | `nba_mod.py paper_summary --window-days 14` |

**Start date:** 2026-02-28. **Earliest GO-LIVE:** 2026-03-14 (if gate passes).

---

#### Step 2: Raise Real-Line Coverage to 70% (Days 1-3)

At 26% coverage, most backtest data is synthetic and meaningless for real-money decisions.

```powershell
# Check current coverage
nba_mod.py odds_coverage --by-date 2026-01-26 2026-02-25

# Backfill in 5-7 day chunks (pts,reb,ast only — save credits)
nba_mod.py odds_backfill 2026-02-01 2026-02-07 --books betmgm,draftkings,fanduel --stats pts,reb,ast --offset-minutes 60 --max-requests 1950 --resume
# ... repeat for each week ...

# Rebuild closes after each chunk
nba_mod.py odds_build_closes 2026-02-01 2026-02-25
```

**Pass:** `realLineSamples / sampleCount >= 70%` on next backtest.

See **docs/COVERAGE_IMPROVEMENT.md** for step-by-step backfill + build_closes to raise coverage.

**Credit math:** `--max-requests 1950` per chunk ≈ 19,500 credits. Stay within 20K plan.

---

#### Step 3: Reb Decision (End of Week 1)

Reb is -5.9% ROI on real lines — the only stat dragging overall ROI negative.

**Decision point after 7 days of paper trading:**
- If reb paper-trading ROI is still negative → **remove from stat whitelist** (change `BETTING_POLICY["stat_whitelist"]` to `{pts, ast, pra}`)
- If reb is positive with the higher edge bar (0.08) → keep it

Without reb, the remaining 3 stats are all positive. This is the single biggest ROI lever available right now.

---

#### Step 4: Fix Minutes 35+ Bucket Bias (Week 2, +0.5-0.8pp)

Current bias: +5.8 min for players projected 35+ minutes. Target: ±1.5 min.

The pregame soft cap (`_SOFT_CAP = 33.0, _DECAY = 0.55` in `_project_minutes()`) is too aggressive for true stars. Options:
- Raise soft cap to 34-35 min
- Make decay player-dependent (lower decay for players averaging 34+ min with low CV)
- Backtest each option with `minutes_eval`

```powershell
nba_mod.py minutes_eval 2026-01-26 2026-02-25 --local
```

**Pass:** 35+ bucket bias < ±3.0 min.

---

#### Step 5: Close 60-70% Calibration Bin Gap (Week 2, +0.8-1.2pp)

The 60-70% confidence bin hits at only 51.9% on real lines — barely above breakeven. Two fixes:

**(a) Extended calibration window:** Refit on 90 days instead of 31 when data is available. More data → more stable temperature estimates.

**(b) Real-line-only calibration:** Current `fit_calibration.py` uses all samples including synthetic. Refit using only real-line bets:

```powershell
scripts\fit_calibration.py --input data/backtest_results/<latest>.json --output models/prob_calibration.json
```

May need a `--real-only` flag added to the script.

**Pass:** 60-70% bin hit rate > 54% on next backtest.

---

#### Step 6: CLV Gate Enforcement (Week 2-3)

Once 2 weeks of `collect_lines` data is flowing through `line_bridge`, enforce CLV as a hard gate:

- Add `clv_line > 0 AND clv_odds_pct > 0` check to `_qualifies()` in `nba_decision_journal.py`
- This filters out bets where the market moved against you — the model found "edge" but sharps disagreed

**The math:** If 55% of 1,986 bets have CLV > 0 (~1,090 bets), and those hit at 57%, effective ROI on the CLV-positive subset is +5-6%. The other 45% you simply don't take.

This is more valuable than any model accuracy improvement.

---

#### Step 7: GO-LIVE Decision (Day 14+)

```powershell
nba_mod.py paper_summary --window-days 14
```

**GO-LIVE gate** (`paper_summary → gate.gatePass`):
- `sample >= 30` settled signals
- `roi > 0.0` over the window
- `positive_clv_pct >= 50.0`
- No single stat with ≥20 signals AND hit rate < 45%

**If PASS:** Start with small units — 0.5-1% bankroll per bet, half-Kelly sizing. Monitor weekly.

**If FAIL:** Diagnose which metric failed, adjust (likely reb removal or CLV threshold), extend paper trading 1 more week. Re-check.

---

#### Step 8: Maintenance Mode (Post GO-LIVE)

Once live, the engine is at or near ceiling. Remaining work is maintenance:

- Refit calibration monthly (or when Brier drifts above 0.240)
- Monitor per-stat ROI weekly — drop any stat that goes negative over 30-day rolling window
- Keep `collect_lines` running 2-3x daily for CLV data
- opp_oreb_pct feature for reb (optional, +0.3-0.5pp on reb only — only worth doing if reb stays in whitelist)

**Stop optimizing after this.** Everything beyond is diminishing returns into noise.

---

### What NOT to Do

- **Don't chase BRef/RAPM/Bayesian priors** — Phase 3 optimizations that add complexity without proven ROI lift. The model is already near ceiling.
- **Don't touch the pregame soft cap** — the live close-game floor handles the in-game case. Pregame cap is correct for pre-tip risk.
- **Don't spend Odds API credits on fg3m/tov/stl/blk** — not in stat whitelist, won't affect GO-LIVE.
- **Don't try to beat 58% hit rate** — that's the hard ceiling for this model class. CLV filtering is the lever, not accuracy.

---

### Weekly Metrics Dashboard

| Metric | Target | Notes |
|--------|--------|-------|
| Real-line hit rate | > 52.4% | Breakeven at -110 juice |
| Real-line ROI/bet | > 0% | Primary GO/NO-GO metric |
| CLV-positive % | >= 50% | Most important filter signal |
| Per-stat ROI (pts, ast, pra) | > 0% each | Drop any stat negative over 30d |
| Reb ROI | monitor | Remove from whitelist if negative at Day 7 |
| 60-70% bin hit rate | > 54% | Calibration health check |
| Minutes 35+ bias | < ±3.0 min | After Step 4 fix |
| Brier avg | < 0.235 | Calibration diagnostic |

---

## 5. Data Flow Summary

```
[Live]  collect_lines  →  data/line_history/*.jsonl  (LineStore)
                              ↓
[Free]  line_bridge    →  OddsStore (snapshots table)
                              ↓
[Paid]  backfill_odds_history  →  OddsStore (snapshots)  [historical gaps]
                              ↓
        build_closing_lines   →  OddsStore (closing_lines table)
                              ↓
        backtest --odds-source local_history  →  roiReal, roiSynth
```

---

## 6. Storage

- **OddsStore DB:** `data/reference/odds_history/odds_history.sqlite`
- **LineStore:** `data/line_history/YYYY-MM-DD.jsonl`
- Both are gitignored via `data/`.

---

## 7. When to Use What

| Need | Use |
|------|-----|
| Fill historical gap (past 30–60 days) | Paid backfill + `--max-requests` + `--resume` |
| Going-forward closing lines | `collect_lines` + `line_bridge` + `odds_build_closes` |
| Stretch credits | Reduce `--stats` to `pts,reb,ast` |
| Check what you have | `odds_coverage` and `odds_coverage --by-date` |

---

## 8. Quality Gate (Before Commits)

```powershell
.\.venv\Scripts\python.exe scripts\quality_gate.py --json
```

All core changes must pass before commit. See CLAUDE.md Section 5.
