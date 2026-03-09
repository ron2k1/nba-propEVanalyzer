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
- ~~Block 40–60% confidence bins~~ ✅ `BETTING_POLICY["blocked_prob_bins"] = {1,2,3,4,5,6,7,8}` (narrowed to bins 0+9 only, 2026-03-03)
- ~~Stat whitelist~~ ✅ `BETTING_POLICY["stat_whitelist"] = {"pts", "ast"}` (reb removed 2026-02-28)
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
- `SIGNAL_SPEC` aligned with `BETTING_POLICY`: stat whitelist `{pts, ast}`, blocked bins `{1,2,3,4,5,6,7,8}` (active: 0+9 only), min edge 0.08, min confidence 0.60
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

---

## Phase 7: Model Improvement — Target 65-70% Real-Line Hit Rate

> **Implemented 2026-03-01.** Changes done step-by-step with backtest validation gates.
> Baseline: 30d Jan 26–Feb 25, bins {1,2,3,4,5,6,7,8} blocked → active bins 0+9 only (narrowed 2026-03-03).

### 7.0 — Correctness Fixes Applied (2026-03-01)

| Bug | Fix Applied |
|-----|------------|
| Pinnacle gate used `noVigUnder` always | Now uses recommended side's no-vig (`noVigOver` for over, `noVigUnder` for under) |
| Pinnacle blocked all backtests | Gate skips when `referenceBook` absent — callers must pass `reference_book="pinnacle"` for live enforcement |
| No per-stat Pinnacle thresholds | `pinnacle_min_no_vig_by_stat: {pts:0.62, ast:0.67, reb:0.62}` added to SIGNAL_SPEC |
| Intraday CLV computed from 1 snapshot | Requires ≥2 distinct timestamps for (player, stat, book, date) |
| `auto_sweep` used global projection for `recentHighVariance` | Now uses stat-level projection dict |
| No `source` field in context_json | All signals include `source: "prop_ev"/"auto_sweep"/"roster_sweep"` |
| `reb` alignment undocumented | `reb` is signal-eligible (calibration data) but BETTING_POLICY blocks betting |

### 7.1 — Signal Quality Hardening (Phase 1)

| Sub-phase | File | Change | Backtest Validation |
|-----------|------|--------|-------------------|
| 1a Pinnacle hard gate | `core/nba_decision_journal.py` | `require_pinnacle`, `pinnacle_thresholds`, `pinnacle_min_no_vig_by_stat` in SIGNAL_SPEC | Paper trade only — no historical Pinnacle data |
| 1b referenceBook in context_json | `nba_cli/ev_commands.py` | `context={"referenceBook":..., "source":..., "recentHighVariance":...}` passed to `log_signal()` | n/a (context storage) |
| 1c Intraday CLV injection | `nba_cli/ev_commands.py` | ≥2 snapshots → `intradayClvLine` in context_json | Paper trade only |

**Pinnacle gate behavior:**
- `referenceBook` absent → gate skips (backtest compat; live callers must always pass `reference_book="pinnacle"`)
- `referenceBook` present + `noVigFor[rec_side]` below threshold → `(False, "pinnacle_X_too_low")`
- `referenceBook` present + threshold met → pass
- Per-stat thresholds override global bin thresholds when set

### 7.2 — Volume Expansion via roster_sweep (Phase 2)

| Sub-phase | File | Change |
|-----------|------|--------|
| 2a `roster_sweep` command | `nba_cli/scan_commands.py` (new) | Scans LineStore snapshots, journals qualifying signals; `source:"roster_sweep"` in context |
| 2b `recentHighVariance` filter | `core/nba_decision_journal.py` | `block_high_variance: True` in SIGNAL_SPEC; blocks unstable-role players |

**roster_sweep notes:**
- Deduplicated to best book per (player, stat) by priority: betmgm > draftkings > fanduel
- Requires `opponent_abbr` in snapshot — only works when `collect_lines` captures opponent info
- Pinnacle gate applies: roster_sweep signals without Pinnacle will pass through (expected — no Pinnacle in LineStore)
- Signal source tracked in context_json for post-launch ROI breakdown by source
- **Volume risk:** roster_sweep may surface noisier lines than auto_sweep. Monitor `signal source` × `hitRate` in journal after 7 days. If `roster_sweep` signals underperform `auto_sweep` signals, add `min_books_offering: 2` gate.

### 7.3 — Calibration Refit on Active Bins (Phase 3)

| Sub-phase | File | Change |
|-----------|------|--------|
| 3a `--max-pred` flag | `scripts/fit_calibration.py` | `fit_bin_temperatures()` now respects `min_pred`/`max_pred`; run with `--max-pred 0.25` for bins 0-1 |
| 3b Opponent B2B | `core/nba_prep_projection.py` | `opponent_is_b2b=False` param; +1.5% pts/reb/ast when True; source: opponent schedule |

**Calibration refit commands:**
```powershell
# Active-bin refit (bins 0-1 only: probOver 0-25%)
.\.venv\Scripts\python.exe scripts\fit_calibration.py \
    --input data/backtest_results/2026-01-26_to_2026-02-25_full_local.json \
    --output models/prob_calibration_bins01.json \
    --max-pred 0.25 --min-pred 0.00 --min-count 20 --bin-min-count 10

# Validate Brier on held-out Jan 10–25 set (NOT used in fitting)
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-01-10 2026-01-25 --model full --local --save
```

**Opponent B2B data source:** same `is_b2b` field from `get_todays_games()` — check opponent team's entry, not the player's team. Callers pass `opponent_is_b2b=` explicitly; no auto-lookup inside projection.

### 7.4 — Game Total Context Signal (Phase 4)

| Sub-phase | File | Change |
|-----------|------|--------|
| 4a Game total integration | `core/nba_prep_projection.py` | `game_total=None` param; ±0.75%/5pt deviation from 226 avg (pts only, capped ±7%) |
| 4a `get_game_total()` stub | `core/nba_data_collection.py` | Returns `None` until totals market added to LineStore |
| 4b `daily_ops` command | `nba_cli/ops_commands.py` (new) | `collect_lines → roster_sweep → best_today`; `--dry-run` skips `log_signal` calls |

**Game total implementation note:** OddsStore stores player props only (pts/reb/ast), NOT h2h game totals. `get_game_total()` returns `None` until `collect_lines` is extended to capture the totals market. When implemented: use Odds API market `totals` (not `h2h`/moneyline). Key: `total` field in event data, stored as `stat="total"` in LineStore.

**`daily_ops --dry-run` definition:** Runs `collect_lines` (writes to LineStore, not journal), then runs roster_sweep and prop evaluation but does NOT call `log_signal()` or `log_prop_ev_entry()`. Prints what signals would have been journaled.

### 7.5 — Backtest Comparison Protocol

**Cannot backtest via historical data:** Pinnacle gate (1a), intraday CLV (1c) — these require live data not in OddsStore.

**Can backtest:** highVariance filter (2b), calibration refit (3a), opponent B2B (3b), game total (4a).

| Phase | Expected bets | Expected roiReal | Expected hitRate | Validation method |
|-------|--------------|-----------------|-----------------|-------------------|
| Baseline (2026-03-01) | 215 | +13.9% | 65.1% | Historical backtest Jan 26–Feb 25 |
| After 2b (highVariance block) | ~180-200 | maintain +13%+ | 66%+ | Historical backtest |
| After 3a (calibration bins01) | same | +14%+ | 66%+ | Brier on Jan 10–25 held-out |
| After 3b (opponent B2B) | same | +14%+ | maintain | Historical backtest |
| After 4a (game total) | same | +15%+ | 67%+ | Historical backtest |
| After 1a (Pinnacle gate) | 100-140 | +18%+ | 68%+ | Paper trade 7d |
| After 2a (roster_sweep) | 400-600 | maintain +15%+ | 65%+ | Paper trade 7d |

**Baseline locked 2026-03-01** (`data/backtest_results/2026-01-26_to_2026-02-25_full_local.json`):

| Metric | Value |
|--------|-------|
| Real-line bets | **202** |
| Real-line hit rate | **63.9%** |
| Real-line ROI | **+11.3%** |
| Synthetic ROI (diagnostic) | +63.4% (97 bets, inflated — ignore) |
| Real-line pts | 82 bets, 54.9% hit, +1.7% ROI |
| Real-line ast | 120 bets, **70.0% hit**, **+17.8% ROI** ← dominant stat |
| Brier (pts/reb/ast/pra) | 0.248 / 0.247 / 0.243 / 0.246 |

**Real-line calibration bins (locked baseline):**

| Bin | probOver range | Bets | Hit% | ROI | Action |
|-----|---------------|------|------|-----|--------|
| 0 | 0-10% | 68 | **70.6%** | **+28.6%** | KEEP — gold zone |
| 1 | 10-20% | 62 | 54.8% | **-3.1%** | IMPROVE — bin 1 is losing |
| 7 | 70-80% | 60 | 61.7% | -0.3% | WATCH — flat on real lines |
| 8 | 80-90% | 11 | 81.8% | +43.6% | SMALL SAMPLE |
| 9 | 90-100% | 1 | 100% | +64.5% | NOISE |

**Key findings:**
- ast is carrying the model: 70% hit rate / +17.8% ROI — protect this
- bin 1 is the main leak: -3.1% ROI despite passing all current gates. Pinnacle gate (Phase 1a) targets this bin directly
- bin 7 (70-80% overs) essentially flat — **now blocked** (bins 1-8 blocked since 2026-03-03)

---

## Phase 8: Unmodeled Signal Gaps (Next Iteration)

> These gaps were identified 2026-03-01 during Phase 7 planning. Each has a defined implementation path. Prioritized by estimated ROI impact vs implementation cost.

### 8.1 — Referee Crew Impact *(+0.5-1.0pp pts, medium cost)*

**Gap:** Some referee crews call 8-12 more fouls/game than average. High-foul refs → more FTs → pts distribution shifts right by ~2-3 pts for stars. Not modeled.

**Implementation:**
- Source: NBA.com referee assignments (available same-day ~3h pre-tip)
- Add `get_referee_crew(game_id)` in `nba_data_collection.py`
- Maintain `data/reference/ref_foul_rates.json` — historical fouls-called per ref from BRef
- In `compute_projection()`: if avg fouls-called for crew > 48/game → apply +1.5% pts multiplier; if < 38 → apply -1.5%
- Backtest validation: split samples by ref crew and compare hit rates

**File:** `core/nba_data_collection.py` + `core/nba_prep_projection.py`
**When:** After game total (Phase 4) is working

### 8.2 — Opponent Key-Defender Injury *(+0.3-0.6pp, low cost)*

**Gap:** When the opponent's primary wing defender (e.g., Kawhi Leonard) is out, the player being projected gets an implicit boost. But team defensive rating updates weekly, not daily — it doesn't reflect today's missing starter.

**Implementation:**
- Already have `fetch_nba_injury_news()` and `get_team_roster_status()` for the player's team
- Call `get_team_roster_status(opponent_abbr)` in `compute_projection()`
- Identify "defensive anchor" = opponent player with highest `defRtg` contribution (proxy: all-defense mentions or usgPct < 20% + top defensive RPM)
- Simpler: if opponent is missing any starter (status=Inactive) at SG/SF position → apply +2% pts/ast multiplier for guards/wings
- Store as `opponentKeyDefenderOut: bool` in context_json

**File:** `core/nba_prep_projection.py`
**When:** After Pinnacle gate paper trading is stable

### 8.3 — Cross-Stat Allocation Damping *(+0.2-0.4pp combo props, low cost)*

**Gap:** Usage adjustment boosts pts AND ast simultaneously when a teammate is out. But pts and ast compete for the same possessions — higher shot volume means fewer assists. Currently `pts_mult` and `ast_mult` are applied independently.

**Implementation:**
- In `compute_usage_adjustment()` (`core/nba_data_prep.py`): after computing individual multipliers, apply damping:
  ```python
  if pts_mult > 1.05 and ast_mult > 1.05:
      # Cross-stat correlation: more shots → fewer assists
      ast_mult = min(ast_mult, pts_mult * 0.88)
  ```
- Correlation coefficient ≈ -0.15 (pts↑ → ast↓ within same usage boost)
- No backtest needed — mathematical correction; validate with 2-3 example player outputs

**File:** `core/nba_data_prep.py`
**When:** Low risk, can implement anytime

### 8.4 — PRA Stdev Correlation *(+0.2-0.3pp pra, medium cost)*

**Gap:** PRA combo stdev currently uses `pra_stdev = f(pts_stdev, reb_stdev, ast_stdev)` as simple sum. True PRA stdev must account for positive within-game correlation (ρ ≈ 0.35-0.45 between pts/reb/ast for same player).

**Implementation:**
- True PRA variance: `var(pra) = var(pts) + var(reb) + var(ast) + 2ρ(pts,reb)·σ_pts·σ_reb + 2ρ(pts,ast)·σ_pts·σ_ast + 2ρ(reb,ast)·σ_reb·σ_ast`
- Estimate ρ from historical game logs for each player (or use fixed ρ=0.35)
- In `_add_combo_projections()` (`nba_prep_projection.py`): replace simple stdev sum with correlated formula
- Expected effect: PRA stdev increases ~15-20% → probabilities move toward 50% → better calibrated pra props

**File:** `core/nba_prep_projection.py` — `_add_combo_projections()`
**When:** After calibration refit (Phase 3a)

### 8.5 — Line Age / Market Efficiency Signal *(+0.3-0.5pp, low cost)*

**Gap:** A line posted 3 days before the game has more projection uncertainty than one posted 2 hours before. The `collect_lines` timestamps allow computing market age. Old lines are less efficient (more edge opportunity); new lines are tighter (market has reacted to news).

**Implementation:**
- In `roster_sweep` / `auto_sweep`: compute `line_age_hours = (now - earliest_snapshot_ts).total_seconds() / 3600`
- Add to context_json as `lineAgeHours`
- In SIGNAL_SPEC: optionally add `max_line_age_hours: 72` — skip signals where line is very fresh (< 4h old, market still reacting) OR very stale (> 96h, injury news may have changed things)
- Not a hard gate — use as confidence weight or informational flag first

**File:** `nba_cli/scan_commands.py` + `core/nba_decision_journal.py`
**When:** After roster_sweep is stable (Phase 2a)

### 8.6 — Season-Phase Momentum *(+0.2-0.4pp, medium cost)*

**Gap:** March NBA games have materially different dynamics that the model ignores:
- Bottom-5 record teams in March → tanking → load management, benching veterans
- Top seeds fighting for home court → full effort even on B2B
- Players near personal milestones (e.g., 2,000 pts season) → extra effort on props

**Implementation:**
- `is_tanking(team_abbr)` → True if team is bottom-5 in conference and eliminated from playoffs
- If `is_tanking` for the *player's team*: apply -5% minutes multiplier (load management risk)
- If `is_tanking` for the *opponent*: apply +2% pts multiplier (easier defense)
- Source: standings from `LeagueStandingsV3` (already in NBA API)

**File:** `core/nba_data_collection.py` + `core/nba_prep_projection.py`
**When:** March-April relevance only; implement before 2026-04-01

### 8.7 — Vig Asymmetry by Book *(+0.1-0.2pp, trivial cost)*

**Gap:** BetMGM charges -115/-115 on some props; DK/FD charge -110/-110. Current no-vig calculation correctly de-vigs, but the EV comparison treats all books equally. A -115 MGM line is priced tighter than a -110 DK line at the same number.

**Implementation (already mostly handled by no-vig math):**
- The no-vig formula `p = implied / (implied_over + implied_under)` already normalizes vig
- Remaining gap: when selecting which book's line to use as the "best" line in `lineShopping`, weight by inverse vig spread (prefer books with lower juice for the same line)
- In `compute_auto_line_sweep()`: add `vig_spread = abs(over_implied + under_implied - 1.0)` per book; prefer lower vig

**File:** `core/nba_prop_engine.py`
**When:** Low risk, 30-minute change

### 8.8 — Prop Market Depth (n_books_offering) *(+0.1-0.2pp, low cost)*

**Gap:** Some player-stat combos are offered by only 1 book. Thin markets have wider error margins. The Pinnacle gate partially addresses this, but when Pinnacle doesn't offer a market, we have no sharp reference.

**Implementation:**
- In `roster_sweep` / `auto_sweep`: count `n_books = len({snap.book for snap in snapshots_for_player_stat})`
- Add to context_json as `nBooksOffering`
- In SIGNAL_SPEC: optionally `min_books_offering: 2` — skip signals where only 1 book has the line
- This is a soft filter until data shows single-book signals underperform

**File:** `nba_cli/scan_commands.py` + `core/nba_decision_journal.py`
**When:** Low risk, add to roster_sweep immediately

### 8.9 — Defensive Rating Recency Window *(+0.2-0.3pp late season, medium cost)*

**Gap:** Defensive ratings and pace factors are computed on full-season data. Teams change significantly after the All-Star break. A team that traded away 2 starters has a different defensive profile in March vs. November, but the season-to-date average smooths this out.

**Implementation:**
- In `get_team_defensive_ratings()`: add `last_n_games=30` option (rolling 30-game window)
- Run `LeagueDashTeamStats` with `LastNGames=30` instead of full season
- Blend: `def_rating = 0.60 * rolling_30g + 0.40 * season_full` (recency-weighted)
- Impact: biggest in late season (Feb-April) when teams are most different from preseason form

**File:** `core/nba_data_collection.py`
**When:** Implement before 2026-03-15 to capture late-season accuracy gains

### 8.10 — Starting Lineup Confirmation Timing *(+0.4-0.8pp, high value)*

**Gap:** Starting lineups are announced ~30-45 minutes before tipoff. A player moving from bench to starter (or starter sitting out) changes minutes projection by 8-12 min. Currently the injury_return G1 cap handles DNPs, but a healthy player "starting for the first time" or "moved to bench" isn't detected.

**Implementation:**
- Add `get_starting_lineup(game_id)` using `BoxScoreStartersV2` or CDN live data
- Compare against previous N-game starter status from game logs
- If new starter (wasn't starting in last 5 games): apply +10% minutes boost
- If demoted to bench (was starting in last 5): apply -15% minutes penalty
- Available ~35 min pre-tip; must run `collect_lines` pass at tipoff time to capture

**File:** `core/nba_data_collection.py` + `core/nba_minutes_model.py`
**When:** High value but requires real-time pipeline; implement after GO-LIVE gate passes

### 8.11 — Blowout Risk via Spread Proxy *(+0.3-0.5pp, medium cost)*

**Gap:** The minutes soft-cap handles the general case but doesn't model *game-specific* blowout probability. A 12-point underdog has a 35%+ chance of 4th-quarter garbage time. Current model treats a 1-point game and a 15-point game the same for minutes projection.

**Implementation:**
- Add `game_spread` param to `compute_projection()` (positive = player's team is favored)
- If `abs(game_spread) > 8`: apply blowout risk multiplier to projected minutes
  - `blowout_mult = 1.0 - max(0, (abs(game_spread) - 8) / 40)` (e.g. spread=12 → mult=0.90)
  - Apply only to players on the favored-by-a-lot team (starters rest) AND the losing team (bench gets garbage time)
- Source: Odds API h2h spreads, or use LineStore if totals market is captured

**File:** `core/nba_prep_projection.py`, `core/nba_data_collection.py`
**When:** After game total pipeline (Phase 4a) is complete — same data source

### 8.12 — Book Stale-Line Cross-Signal *(+0.3-0.5pp, low cost)*

**Gap:** When DraftKings has already moved a line (sharper book reacts first) but BetMGM hasn't updated yet, the BetMGM line is objectively stale relative to the market. `detect_stale_lines()` already identifies this. But it's not connected to `_qualifies()`.

**Implementation:**
- In `roster_sweep`: before calling `compute_prop_ev()`, call `ls.detect_stale_lines(date_str, min_line_diff=0.5)`
- If a stale opportunity exists for (player, stat): add `staleBookSignal: True` + `staleLineDiff` to context_json
- Optionally add `require_stale_or_pinnacle: True` to SIGNAL_SPEC — only log signals that have EITHER Pinnacle confirmation OR a stale-book discrepancy (two independent confirmations)
- This creates a second signal pathway that doesn't require Pinnacle data

**File:** `nba_cli/scan_commands.py` + `core/nba_decision_journal.py`
**When:** After roster_sweep is stable (Phase 2a)

### 8.13 — Pace-Adjusted Projections for Tempo Mismatches *(+0.2-0.4pp, medium cost)*

**Gap:** Current model uses opponent's season-average pace factor. But when a fast team (top-5) plays a slow team (bottom-5), actual game pace isn't either team's average — it's a blended expected pace. The current `paceFactor` from defensive ratings uses the opponent's pace alone, ignoring the player's team pace.

**Implementation:**
- In `compute_projection()`: fetch player's team pace from defensive ratings
- `expected_pace = 0.50 * player_team_pace + 0.50 * opp_team_pace` (true expectation = average)
- `pace_adjustment = expected_pace / league_avg_pace` (league avg ≈ 1.0 by construction)
- Replace `opp_def.get("paceFactor", 1.0)` with this blended expected pace factor
- Largest impact on extreme mismatches (e.g., OKC pace=1.10 vs NYK pace=0.92 → expected=1.01)

**File:** `core/nba_prep_projection.py` → `_defense_adj()`
**When:** Medium complexity; implement after Phase 8.9 (defensive rating recency)

### 8.14 — Home/Away Stat Split Calibration *(+0.3-0.7pp, medium cost)*

**Gap:** The model applies a `homeAway` *minutes* multiplier but projects stats from season-wide averages. A player averaging 30 pts at home vs 22 pts away has a fundamentally different distribution in each context, but the Normal CDF is fitted against the blended 26-pt average. This produces systematic over-projection for road games and under-projection for home games for any player with a split > 4 pts.

**Why it matters now:** The 30d backtest shows pts at +1.7% ROI on 82 bets — thin edge. Correcting home/away bias for pts could push this to +3-4%. BRef home/away splits are already ingested by `nba_bref_data.py`.

**Implementation:**
- In `compute_projection()`: after fetching `bref_stats`, check `bref_stats.get("homeAvg")` and `bref_stats.get("awayAvg")`
- If available and `seasonGP >= 15`: use home avg when `is_home=True`, away avg when `is_home=False` as the base projection input instead of season avg
- Apply the same blending logic (70% model + 30% avg) against the split-specific avg
- Only apply if split differs from season avg by >3 pts (filter noise from small samples)

**File:** `core/nba_prep_projection.py` → `compute_projection()` + `nba_bref_data.py` (add `homeAvg`/`awayAvg` to fetch)
**When:** High value; implement before calibration refit so splits are baked into the fitted temps

---

### 8.15 — Recent Role Change Flag *(+0.3-0.5pp, low cost)*

**Gap:** When a player's last-3-games minutes average deviates from their season minutes by more than 5 min (new starter, coming off bench, minutes restriction), the season-wide stdev estimate is stale. The model's `recentHighVariance` flag catches high variance but not *directional* role shifts. A player who was 28 mpg but is now 36 mpg (became the starter) will be systematically under-projected.

**Why it matters now:** recentHighVariance blocks the signal; role change needs to *update* the projection, not block it.

**Implementation:**
- In `compute_projection()`: compute `role_change_delta = last3_min_avg - season_min_avg`
- If `abs(role_change_delta) > 5.0`: set flag `recentRoleChange = True` and apply a per-minute rate boost/cut: `role_adj = 1.0 + (role_change_delta / season_min_avg) * 0.70` (70% pass-through to stats)
- Use last-5 games as the projection base (not season avg) when `recentRoleChange = True`
- Add `recentRoleChange`, `roleChangeDelta` to projection output dict

**File:** `core/nba_prep_projection.py` → `compute_projection()`, alongside existing `recentHighVariance` logic
**When:** Low cost, implement next

---

### 8.16 — Cross-Book Line Dispersion Signal *(+0.2-0.4pp, low cost)*

**Gap:** When BetMGM, DraftKings, and FanDuel all post the same line (±0.25), the market is efficient — three independent books agreeing is a strong consensus signal. When they disagree by >0.5, the outlier book has a soft line. Currently the model treats all books equally at the same line. A BetMGM line of 27.5 when DK and FD are at 28.0 is not the same quality signal as all three at 27.5.

**Implementation:**
- In `compute_auto_line_sweep()` → ranked offer building: compute `bookLineStdev = stdev([line for each book's offer])` across all offers for the same player/stat
- Add `bookLineStdev` to each ranked item and to the result dict
- In `_qualifies()`: add `max_book_line_dispersion: 0.75` to SIGNAL_SPEC — if `bookLineStdev > 0.75`, add flag `"soft_line_dispersion"` to context_json (don't block, just flag)
- In `best_today` output: show `bookLineStdev` as a signal quality indicator

**File:** `core/nba_prop_engine.py` → ranked offer building + sort; `core/nba_decision_journal.py` → SIGNAL_SPEC
**When:** After roster_sweep is stable (requires multi-book snapshots)

---

### 8.17 — Post-Blowout Game Urgency *(+0.2-0.4pp pts/ast, trivial cost)*

**Gap:** After a team loses by 20+ points, coaches typically emphasize effort, players play with more urgency, and stars take on larger scoring loads. Sports science literature shows +4-8% pts output in the immediate next game after a blowout loss. The opposite (blowout win) tends to produce rest/coasting.

**Implementation:**
- In `compute_projection()`: accept `prior_game_margin: int = 0` param (negative = loss, positive = win)
- Compute `blowout_adj`:
  - `prior_game_margin <= -20`: `blowout_adj = 1.04` (team urgency after blowout loss)
  - `prior_game_margin >= +25`: `blowout_adj = 0.97` (coasting after blowout win)
  - Otherwise: `blowout_adj = 1.0`
- Apply only to pts and ast (not reb — reb is more possession-driven than effort)
- Data source: `player_game_log` already fetched — `prior_game_margin = game_log[1]["plusMinus"]` if available (game_log[0] = current game, [1] = previous)

**File:** `core/nba_prep_projection.py` → `compute_projection()`; data available via game log
**When:** Low cost, implement alongside 8.15

---

### 8.18 — 3-in-4 Nights Fatigue Compounding *(+0.2-0.4pp, low cost)*

**Gap:** The current B2B multiplier treats all back-to-back games equally. But in the NBA's compressed schedule, a player sometimes plays a 3rd game within 4 nights (3-in-4 scenario). The fatigue effect on night 3 of 4 is ~1.5x the normal B2B effect. Additionally, a *traveling* B2B (flew to a different city the night before) is harder than a *home-home* B2B. Both are collapsed into the same `is_b2b = True` flag.

**Implementation:**
- In `get_todays_games()` or schedule fetch: detect 3-in-4 pattern by checking if the player played games on both D-3 and D-1 relative to today
- Add `daysRestCount: int` and `is3in4: bool` to game context
- In `compute_projection()`: if `is3in4 = True`: apply `b2b_multiplier ** 1.5` instead of `b2b_multiplier ** 1.0` for the fatigue term
- Travel B2B: if `prior_game_location != today_game_location` (different arena city): apply additional -0.5% multiplier

**File:** `core/nba_prep_projection.py` → B2B adjustment block; `core/nba_data_collection.py` → schedule fetch
**When:** Medium complexity, implement before Phase 3b (opponent B2B)

---

### 8.19 — Denver Altitude Pace Boost *(+0.1-0.2pp pts/ast for DEN home games, trivial cost)*

**Gap:** Ball Arena (Denver) is at 5,280 ft elevation. Visiting teams experience measurable fatigue and reduced defensive intensity in the 3rd/4th quarters. Historically, Denver home games average +3-4 pts per team above expected pace/scoring. This is not captured by defensive ratings (which average home and away games together).

**Implementation:**
- In `compute_projection()`: check `if home_team == "DEN" and not is_home` (visiting player at Denver)
- Apply `altitude_mult = 1.025` to pts and ast (visiting player benefits from Denver pace, not just DEN players)
- Also apply to the DEN home player: `altitude_mult = 1.02` (home team also scores more in altitude games)
- Simple dict lookup: `_ALTITUDE_BOOST = {"DEN": 0.025}` for home games

**File:** `core/nba_prep_projection.py` → `compute_projection()`, add altitude lookup after pace adjustment
**When:** Trivial cost (15 min), implement with 8.17

---

### 8.20 — AST Structural Book Underpricing Detection *(+0.5-1.0pp ast, medium cost)*

**Gap:** The backtest shows ast at +17.8% ROI on 120 bets, 70% hit rate — an unusually large and consistent edge. This suggests a structural mispricing by books on assist lines. The question is: is this edge concentrated in specific books (e.g., DK sets systematically lower ast lines), specific players (playmakers who set up offense), or across the board? Detecting and confirming the source allows us to:
1. Increase bet confidence on ast for those specific book/player combinations
2. Watch for the edge closing (books adjusting their ast models)

**Implementation:**
- Add `clv_by_stat_book` analysis to `clv_eval` command: compute avg CLV and hit rate segmented by `(stat, book)` over last 30d
- If a (stat, book) pair shows persistent CLV > 0 with hit rate > 65% over 30+ samples: flag as `structural_edge` in context_json
- In `best_today` output: mark flagged (stat, book) pairs with `[EDGE]` indicator
- Track edge decay: if the CLV advantage drops below 0.02 over a 14d trailing window, emit `edge_closing` alert

**File:** `nba_cli/line_commands.py` → `clv_eval`; `core/nba_decision_journal.py` → context enrichment
**When:** After 30d of paper trading data accumulates (requires journal entries with closing lines)

---

### 8.21 — Small-Sample Projection Confidence Penalty *(+0.1-0.3pp, low cost)*

**Gap:** A player with 12 games has the same projection confidence as one with 55 games in the current model. But small samples have wider true uncertainty — the stdev estimate from 12 games has a confidence interval 2x wider than from 40 games. This means we're under-weighting uncertainty for new players and over-accepting borderline edges for players who just came back from injury.

**Implementation:**
- In `compute_projection()`: compute `sample_confidence_factor = min(1.0, seasonGP / 30)` (full confidence at 30+ games)
- Inflate `projStdev` by `stdev * (1.0 + (1 - sample_confidence_factor) * 0.25)` — max +25% wider stdev at 1 game, scales to 0% inflation at 30 games
- Add `sampleConfidenceFactor` to projection output dict
- Only apply when `seasonGP < 30`

**File:** `core/nba_prep_projection.py` → stdev computation block
**When:** Low cost, implement alongside 8.15

---

### 8.22 — Rest Advantage Interaction Term *(+0.2-0.4pp, low cost)*

**Gap:** `is_b2b` captures the player's own fatigue but not the asymmetry when a well-rested player faces a B2B opponent. A player on 3+ days rest vs a team on 0 days rest produces a larger edge than either effect alone. Currently these are independent inputs; the interaction (well_rested × opp_b2b) is not computed.

**Implementation:**
- In `compute_projection()`: add `opponent_is_b2b` is already a param (Phase 3b). Add interaction term:
  ```python
  _rest_days = 1 if not is_b2b else 0
  _rest_advantage = _rest_days >= 2 and opponent_is_b2b  # player rested, opp tired
  if _rest_advantage and stat in ("pts", "ast"):
      stat_mult = max(lo, min(hi, stat_mult * 1.025))
  ```
- Data source: `is_b2b` and `opponent_is_b2b` both already available in `compute_projection()` signature.

**File:** `core/nba_prep_projection.py` → per-stat loop, after existing B2B block
**When:** Low cost; implement next bundle

---

### 8.23 — Hot Streak Persistence Flag *(+0.2-0.4pp pts, low cost)*

**Gap:** The model uses `last5Avg` and `last10Avg` but doesn't detect directional streaks. A player who has gone OVER their line 6 of the last 8 games in the same stat is in a fundamentally different state than one who has gone 4/8. Empirically, scoring streaks in high-usage players (usgPct >= 25%) persist at 56-60% one-game forward. Cold streaks regress faster (mean-reversion kicks in after 4+ unders in a row).

**Implementation:**
- Compute `over_rate_l8 = sum(1 for g in logs[:8] if g.get(stat, 0) >= season_avg) / 8`
- If `over_rate_l8 >= 0.75` (6+ of last 8 over): `streak_mult = 1.03` (hot streak continuation)
- If `over_rate_l8 <= 0.25` (2 or fewer of last 8 over): `streak_mult = 0.98` (mean reversion)
- Else: `streak_mult = 1.0`
- Apply only to pts and ast; only when `len(logs) >= 8`
- Add `streakMultApplied` to projection output

**File:** `core/nba_prep_projection.py` → per-stat loop
**When:** Low cost, implement with 8.22

---

### 8.24 — Line Setting Age / Time-to-Game Signal *(+0.2-0.4pp, medium cost)*

**Gap:** Lines posted 3+ days before game time are set conservatively — books don't have full injury/lineup data. Lines posted 24 hours or less before game contain more current market intelligence. A line we first see 3+ days out at 26.5 that hasn't moved by game day has different information content than one posted yesterday at 26.5. The `lineAge` (hours since first snapshot) is computable from LineStore.

**Why it matters:** In backtest, the real closing line is used. But for live betting, we often see lines 2-3 days in advance. If we beat the line early (CLV positive), that's valuable. If we enter late after the line has moved against us, the edge is narrower.

**Implementation:**
- In `_qualifies()` / SIGNAL_SPEC: add `max_line_age_hours: 72` — if `context_json["lineAgeHours"] > 72`, reduce confidence score (don't block, just flag)
- In `ev_commands.py`: compute `lineAgeHours` from first vs current LineStore snapshot timestamps; store in context_json
- In `best_today`: show `lineAgeHours` in output per signal

**File:** `core/nba_decision_journal.py` SIGNAL_SPEC; `nba_cli/ev_commands.py` context building
**When:** Requires LineStore snapshots (live pipeline only, not backtest)

---

### 8.25 — Clutch Usage vs Regular-Time Rate *(+0.3-0.6pp pts, medium cost)*

**Gap:** Some players (closer archetype: Damian Lillard, SGA, Tatum) see dramatically higher usage in the last 5 minutes of close games (+15-25% pts rate vs regular time). Others see equal or lower usage (role players substituted out). The current projection uses a uniform per-minute rate across the full game. For star players, this under-values their total pts contribution.

**Implementation:**
- From BRef or NBA API: fetch clutch stats (last 5 min, margin ≤5) vs non-clutch stats
- `clutch_pts_rate = clutch_pts / clutch_min` vs `regular_pts_rate = (pts - clutch_pts) / (min - clutch_min)`
- `clutch_adjustment = (clutch_pts_rate / regular_pts_rate - 1.0) * expected_clutch_min_fraction`
- Expected clutch min fraction ≈ 0.12 (about 4-5 of 40 minutes in close games)
- Apply: `final_projection += final_projection * clutch_adjustment * 0.50` (50% weight, game-script uncertain)

**File:** `core/nba_prep_projection.py`; `core/nba_data_collection.py` (clutch stat fetch)
**When:** Medium complexity; after 8.23 verified; pts-only initially

---

### Phase 8 Priority Order

**Backtest snapshots (Jan 26–Feb 25, real closing lines):**

| Checkpoint | Bets | Hit Rate | roiReal | pts ROI | ast ROI |
|------------|------|----------|---------|---------|---------|
| Baseline (bins 0-1 only) | 202 | 63.9% | +11.28% | +1.69% | +17.83% |
| After 8.15/8.17/8.19 | 212 | 64.2% | +11.66% | +5.96% | +15.26% |
| After 8.14/8.12/8.8 + refactor | **213** | **64.8%** | **+12.80%** | +4.79% | **+17.72%** |

Key takeaway: cumulative improvement of +1.52pp roiReal vs baseline on +11 bets. ast returned to +17.7% (refactor confirmed no regression). pts stable at +4.8%. Bin 1 continues improving trend.

| Priority | Gap | Est ROI Impact | Implementation Cost | When |
|----------|-----|----------------|--------------------|----|
| 1 | 8.3 Cross-stat allocation damping | +0.2-0.4pp | 30 min | ✅ Done |
| 2 | 8.7 Vig asymmetry (book selection) | +0.1-0.2pp | 30 min | ✅ Done |
| 3 | 8.15 Role change flag | +0.3-0.5pp | 1 hour | ✅ Done |
| 4 | 8.17 Post-blowout urgency | +0.2-0.4pp | 1 hour | ✅ Done |
| 5 | 8.19 Denver altitude boost | +0.1-0.2pp | 15 min | ✅ Done |
| 6 | 8.14 Home/Away stat splits | +0.3-0.7pp | 3 hours | ✅ Done |
| 7 | 8.12 Stale-book cross-signal | +0.3-0.5pp | 1 hour | ✅ Done (diagnostic) |
| 8 | 8.8 n_books_offering gate | +0.1-0.2pp | 1 hour | ✅ Done |
| 9 | 8.22 Rest advantage interaction | +0.2-0.4pp | 30 min | Next bundle |
| 10 | 8.23 Hot streak persistence | +0.2-0.4pp | 1 hour | Next bundle |
| 11 | 8.18 3-in-4 nights fatigue | +0.2-0.4pp | 2 hours | After schedule data confirmed |
| 12 | 8.16 Cross-book line dispersion | +0.2-0.4pp | 2 hours | After roster_sweep stable |
| 13 | 8.5 Line age signal | +0.3-0.5pp | 2 hours | After roster_sweep stable |
| 14 | 8.24 Line setting age signal | +0.2-0.4pp | 2 hours | After LineStore stable |
| 15 | 8.25 Clutch usage rate | +0.3-0.6pp | 4 hours | After 8.23 verified |
| 16 | 8.20 AST book underpricing detection | +0.5-1.0pp | 3 hours | After 30d journal data |
| 17 | 8.2 Opponent key-defender injury | +0.3-0.6pp | 3 hours | After Pinnacle paper stable |
| 18 | 8.4 PRA stdev correlation | +0.2-0.3pp | 4 hours | After calibration refit |
| 19 | 8.11 Blowout risk via spread | +0.3-0.5pp | 4 hours | After game total pipeline |
| 20 | 8.13 Pace tempo mismatch | +0.2-0.4pp | 3 hours | After def rating recency |
| 21 | 8.9 Defensive rating recency | +0.2-0.3pp | 4 hours | Before 2026-03-15 |
| 22 | 8.1 Referee crew impact | +0.5-1.0pp | 6 hours | After game total working |
| 23 | 8.6 Season-phase momentum | +0.2-0.4pp | 5 hours | Before 2026-04-01 |
| 24 | 8.10 Starting lineup confirmation | +0.4-0.8pp | 8 hours | After GO-LIVE |

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
