# Roadmap

## Milestone 1: GO-LIVE (Target: 2026-03-14)

All phases 1–5 are complete. Phase 6 is the final gate.

---

## Phase 6: Paper-Trading + GO-LIVE Validation (ACTIVE)

**Start:** 2026-02-28
**Earliest GO-LIVE:** 2026-03-14

### Step 1 — Paper-Trading + CLV Accumulation (Days 1–14, ONGOING)

**Highest-leverage activity. Everything else is secondary.**

Daily routine (every game day):

| When | Command |
|------|---------|
| 11am ET | `nba_mod.py collect_lines --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra` |
| 2pm ET | same |
| 5pm ET | same |
| 5pm ET | `nba_mod.py best_today 20` |
| 11pm ET | `nba_mod.py line_bridge --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra` |
| 11pm ET | `nba_mod.py odds_build_closes` |
| Next AM | `nba_mod.py paper_settle <yesterday>` |
| Next AM | `nba_mod.py results_yesterday 50` |
| Next AM | `nba_mod.py paper_summary --window-days 14` |

**Pass:** 30+ settled signals, ROI > 0%, CLV-positive ≥ 50%.

---

### Step 2 — Raise Real-Line Coverage to 70% (Days 1–3)

Current coverage: 26% (8,097 / 30,728). Most backtest P&L is meaningless at this level.

```powershell
# Check current coverage
nba_mod.py odds_coverage --by-date 2026-01-26 2026-02-25

# Backfill in weekly chunks (pts,reb,ast only — save credits)
nba_mod.py odds_backfill 2026-02-01 2026-02-07 --books betmgm,draftkings,fanduel --stats pts,reb,ast --offset-minutes 60 --max-requests 1950 --resume
# ... repeat for each week through 2026-02-25 ...

# Rebuild closes after each chunk
nba_mod.py odds_build_closes 2026-02-01 2026-02-25

# Re-run 30d backtest
nba_mod.py backtest 2026-01-26 2026-02-25 --model full --local --odds-source local_history --save
```

**Pass:** `realLineSamples / sampleCount >= 70%`

---

### Step 3 — Reb Decision (End of Week 1, ~2026-03-07)

Reb is -5.9% ROI on real lines — only stat dragging ROI negative.

- If reb paper ROI still negative after 7 days → remove from stat whitelist (`{pts, ast, pra}`)
- If reb positive with 0.08 edge bar → keep it

Without reb, remaining 3 stats are all profitable. This is the single biggest ROI lever.

---

### Step 4 — Fix Minutes 35+ Bucket Bias (Week 2, ~2026-03-07 to 2026-03-14)

Current bias: +5.8 min for 35+ players. Target: ±1.5 min.

Options:
- Raise soft cap (`_SOFT_CAP`) from 33 to 34–35 min
- Make decay player-dependent (lower for players avg 34+ min, low CV)

```powershell
nba_mod.py minutes_eval 2026-01-26 2026-02-25 --local
```

**Pass:** 35+ bucket bias < ±3.0 min.
**Expected gain:** +0.5-0.8pp hit rate.

---

### Step 5 — Close 60-70% Calibration Bin Gap (Week 2, ~2026-03-07 to 2026-03-14)

Current 60-70% bin hits at 51.9% on real lines (barely above breakeven).

Options:
- Refit calibration on 90-day window when available
- Refit using real-line-only bets (`--real-only` flag needed in `fit_calibration.py`)

```powershell
scripts\fit_calibration.py --input data/backtest_results/<latest>.json --output models/prob_calibration.json
```

**Pass:** 60-70% bin hit rate > 54%.
**Expected gain:** +0.8-1.2pp hit rate.

---

### Step 6 — CLV Gate Enforcement (Week 2-3, ~2026-03-07 to 2026-03-14)

Once 2 weeks of `collect_lines` data flows through `line_bridge`, add CLV as a hard gate:

- Add `clv_line > 0 AND clv_odds_pct > 0` to `_qualifies()` in `nba_decision_journal.py`

**The math:** If 55% of bets have CLV > 0 and those hit at 57%, effective ROI on CLV-positive subset is +5-6%. The other 45% you simply don't take.

**More valuable than any model accuracy improvement.**

---

### Step 7 — GO-LIVE Decision (Day 14+, ~2026-03-14)

```powershell
nba_mod.py paper_summary --window-days 14
```

**PASS → GO-LIVE:** 0.5-1% bankroll per bet, half-Kelly sizing.
**FAIL → Extend paper trading 1 week, diagnose failure metric.**

---

## Phase 7: Maintenance Mode (Post GO-LIVE)

- Refit calibration monthly (or when Brier drifts above 0.240)
- Monitor per-stat ROI weekly — drop any stat negative over 30d rolling
- Keep `collect_lines` running 2-3x daily for CLV data
- opp_oreb_pct feature for reb (optional, +0.3-0.5pp — only if reb stays in whitelist)
- **STOP optimizing after GO-LIVE.** Everything beyond is diminishing returns into noise.

---

## Completed Phases

| Phase | What | Result |
|-------|------|--------|
| 1 | Real-line coverage ≥ 70% | Done |
| 2 | Calibration + ROI-by-bin | Done |
| 3 | Model math (no-vig, regression, variance) + datasets | Done |
| 4 | Minutes model (starter, blowout, trend) | Done; MAE 5.66→5.63 |
| 5 | Betting gate (EV 0.05, block 40–60%, whitelist) | Done; ROI -3.77%→+0.088% |
