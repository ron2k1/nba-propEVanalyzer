# Overfitting Verification Report — 2026-03-02

## Executive Summary

**The claimed +19.6% OOS ROI (Oct 21–Nov 30) and +32.03% in-sample ROI (Dec 28–Feb 25) are both artifacts of contamination. The true reproducible performance is ~+0.1% to +3.8% ROI on real closing lines across all periods tested.**

Four parallel backtests were run in isolated git worktrees to quantify each contamination vector.

---

## Results Table

| Scenario | Period | Bets | Hit% | roiReal | Delta vs Contaminated |
|----------|--------|------|------|---------|-----------------------|
| **Contaminated OOS** | Oct 21–Nov 30 | 384 | 65.6% | +19.6% | baseline (claimed) |
| No calibration | Oct 21–Nov 30 | 1,175 | 57.9% | **+3.78%** | -15.8pp |
| Old policy (cal on) | Oct 21–Nov 30 | 3,798 | 55.3% | **+0.13%** | -19.5pp |
| Clean OOS (no contam.) | Oct 21–Nov 30 | 3,798 | 55.3% | **+0.13%** | -19.5pp |
| **Claimed in-sample** | Dec 28–Feb 25 | 283 | 72.09% | +32.03% | (claimed) |
| **Reproduced in-sample** | Dec 28–Feb 25 | 1,079 | 58.4% | **+3.76%** | NOT REPRODUCIBLE |
| 60d log entry #1 | Dec 28–Feb 25 | 4,110 | 55.8% | **+1.40%** | actual run on 2026-03-01T00:36 |
| 60d log entry #2 | Dec 28–Feb 25 | 3,573 | 54.9% | **+0.35%** | actual run on 2026-03-01T08:36 |

---

## Contamination Vectors Quantified

### 1. Calibration Lookahead: ~16pp ROI inflation
- Temps fitted on Dec 1–Feb 25 data, applied retroactively to Oct–Nov
- Removing calibration: 384 bets/+19.6% → 1,175 bets/+3.78%
- Mechanism: temperature scaling shifts raw probabilities into the "active" bins (0-10%, 10-20%), concentrating bets into a smaller, higher-quality subset
- **However:** clean-oos shows calibration has zero marginal effect with old (wider) policy — the 16pp effect is entirely an interaction between calibration and tight bin filtering

### 2. Policy Snooping: ~19.5pp ROI inflation
- stat_whitelist narrowed from {pts,reb,ast,pra} → {pts,ast} based on Dec–Feb ROI
- blocked_prob_bins expanded from {2-6} → {2-7} based on Dec–Feb bin analysis
- min_edge_threshold raised from 0.05 → 0.08 based on Dec–Feb real-line data
- Old policy: 3,798 bets/+0.13% → Current policy: 384 bets/+19.6%
- **The policy changes are correct** — reb/pra/bin-7 ARE negative-ROI on OOS data too — but applying them retroactively inflates the filtered result

### 3. Combined Effect (Clean OOS)
- No calibration + old policy = 3,798 bets at +0.13%
- **Identical to old-policy-only** — calibration adds nothing when the policy is already wide
- The 19.5pp gap is almost entirely driven by cherry-picking profitable stat+bin subsets post-hoc

### 4. In-Sample Baseline: NOT REPRODUCIBLE
- Claimed: 283 bets / 72.09% hit / +32.03% ROI
- Reproduced with current code: 1,079 bets / 58.4% hit / +3.76% ROI
- 60d log from March 1: 3,573–4,110 bets / 54.9-55.8% hit / +0.35-1.40% ROI
- **The 283-bet number was never the actual backtest output.** It appears to have been a manually curated subset (bins 0+1+9 only, ~166+104+13 bets) possibly from a period when the odds database had fewer closing lines

---

## Per-Stat Analysis (Clean OOS: Oct 21–Nov 30, no contamination)

| Stat | Bets | Hit% | ROI | Verdict |
|------|------|------|-----|---------|
| pts | 871 | 53.0% | -1.28% | Losing on OOS real lines |
| reb | 706 | 56.4% | +0.68% | Marginal (correctly removed from whitelist) |
| ast | 966 | 60.7% | **+4.85%** | Only profitable stat on OOS real lines |
| pra | 1,255 | 52.3% | -2.84% | Losing (correctly removed from whitelist) |

**ast is the only stat with genuine OOS signal.** The policy correctly identified reb/pra as losers.

## Per-Bin Analysis (Clean OOS)

| Bin | Bets | Hit% | ROI | Verdict |
|-----|------|------|-----|---------|
| 0-10% (UNDER) | 909 | 59.9% | **+9.80%** | Real edge in extreme UNDER tail |
| 10-20% (UNDER) | 1,067 | 56.0% | +1.46% | Marginal positive |
| 70-80% (OVER) | 1,205 | 54.2% | -2.87% | Losing (correctly blocked) |
| 80-90% (OVER) | 465 | 45.8% | -17.84% | Severely losing |
| 90-100% (OVER) | 152 | 61.8% | +11.70% | Small sample, promising |

**The 0-10% UNDER bin is the engine's genuine edge.** It holds across both OOS and in-sample periods.

---

## Key Findings

### What's Real
1. **The 0-10% bin (extreme UNDER) has genuine predictive power** — +9.80% ROI on 909 uncontaminated OOS bets
2. **ast is the only profitable stat** across both periods on real lines
3. **The policy changes were directionally correct** — they correctly identified losing segments (reb, pra, bins 7-8)
4. **The model is not zero-edge** — even fully decontaminated, it produces ~+0.1-4% ROI depending on filtering

### What's Not Real
1. **+19.6% OOS ROI** — contaminated by post-hoc calibration + policy; true OOS is +0.13%
2. **+32.03% in-sample ROI on 283 bets** — not reproducible; actual is +3.76% on 1,079 bets
3. **"True out-of-sample" claims** — Oct–Nov was never OOS; all current parameters were fitted on or after that data
4. **The ~7-13pp inflation estimate** — actual inflation is ~16-19pp, worse than estimated

### The Overfitting Pattern
The engine exhibits classic **data-dredging via progressive filtering**:
1. Run backtest on full data → breakeven (~+0.1%)
2. Identify losing stats → remove them → ROI improves
3. Identify losing bins → block them → ROI improves further
4. Fit calibration to concentrate bets into winning bins → ROI jumps
5. Report the filtered result as if it were the model's performance

Each step is individually defensible but collectively they overfit to the training period.

---

## Recommendations

1. **Correct CLAUDE.md and MEMORY.md** — replace the 283-bet/+32.03% claim with the reproducible 1,079-bet/+3.76% number
2. **Treat Oct–Nov as contaminated** — it is NOT valid OOS. True OOS requires paper-trading on future games
3. **The only honest performance claim** is: "model produces +0.1-4% ROI on real closing lines across Oct–Feb, with edge concentrated in the 0-10% UNDER bin for ast props"
4. **Paper trading is the only valid forward test** — no historical backtest can produce uncontaminated numbers given the iterative tuning that occurred
5. **Consider narrowing to ast-only, bin-0-only** as the highest-conviction strategy (~+9.80% ROI on 909 OOS bets, +4.85% for ast specifically)

---

## Methodology

- 4 parallel agents in isolated git worktrees (no cross-contamination)
- Each agent modified code/files independently, ran backtest, reported results
- All backtests used: `--model full --local --odds-source local_history`
- Date range: Oct 21–Nov 30, 2025 (OOS) and Dec 28–Feb 25 (in-sample)
- No permanent code changes — worktrees discarded after runs
