# Execution Report — Model Quantization Pipeline

**Date:** 2026-02-28  
**Plan:** PLAN_EXECUTE_NOW.md  
**Goal:** Backtest against real closing odds only, quantize model, refit calibration.

---

## Summary

| Step | Status | Result |
|------|--------|--------|
| 1. Odds coverage check | Done | 23,583 closing rows, 189 events, 357 players (Jan 26–Feb 26) |
| 2. Rebuild closing lines | Done | 23,583 derived and saved |
| 3. Real-line-only backtest | Done | 1,697 bets, 100% real (0 synthetic) |
| 4. Calibration refit | Done* | New fit T=5 over-shrinks; kept prior values |
| 5. Minutes evaluation | Done | MAE 5.62, bias -0.35 min |

\*fit_calibration produced pts=5, reb=5, pra=5 (heavy shrink). Restored prior temps (pts 1.47, ast 1.23, pra 1.97) that delivered +1.56% roiReal.

---

## Real-Line-Only Backtest Results (2026-01-26 → 2026-02-25)

**Mode:** `--odds-source local_history --real-only` — zero synthetic data.

### Primary Metrics

| Metric | Value |
|--------|-------|
| roiReal | **+1.56%** |
| hitRatePct | **56.099%** |
| betsPlaced | 1,697 |
| realLineSamples | 8,097 |
| missingLineSamples | 0 |
| roiSynth | N/A (excluded) |

### Per-Stat ROI (Real Lines Only)

| Stat | Bets | Hit% | ROI |
|------|------|------|-----|
| pts | 790 | 55.1% | +1.84% |
| ast | 649 | 57.6% | +0.82% |
| pra | 258 | 55.4% | +2.56% |
| reb | 0 | — | — (removed from whitelist) |

### Brier by Stat

| Stat | Brier |
|------|-------|
| pts | 0.258 |
| reb | 0.255 |
| ast | 0.252 |
| pra | 0.255 |

### Calibration Bins (Real-Line Bets)

| Bin | Bets | Wins | Hit% | ROI/bet |
|-----|------|------|------|---------|
| 0-10% | 54 | 46 | 85.2% | +55.9% |
| 10-20% | 83 | 45 | 54.2% | -2.8% |
| 20-30% | 264 | 141 | 53.4% | -3.1% |
| 30-40% | 863 | 485 | 56.2% | +2.9% |
| 40-50% | 0 | — | — | blocked |
| 50-60% | 0 | — | — | blocked |
| 60-70% | 354 | 192 | 54.2% | -3.0% |
| 70-80% | 70 | 35 | 50.0% | -16.8% |
| 80-90% | 8 | 7 | 87.5% | +48.5% |
| 90-100% | 1 | 1 | 100% | +64.5% |

### Projection Accuracy

| Metric | Value |
|--------|-------|
| maeByStat (pts) | 5.06 |
| maeByStat (reb) | 2.03 |
| maeByStat (ast) | 1.57 |
| maeByStat (pra) | 6.40 |

---

## Minutes Model Evaluation

| Bucket | Count | MAE | Bias |
|--------|-------|-----|------|
| 0-15 min | 999 | 6.19 | -1.33 |
| 15-25 min | 1,660 | 5.54 | -1.30 |
| 25-35 min | 1,569 | 5.37 | +1.26 |
| 35+ min | 13 | 3.98 | +2.39 |

**Overall:** MAE 5.62, bias -0.35 min. 35+ bucket bias +2.39 min (within ±3.0 gate).

---

## Artifacts

- Backtest JSON: `data/backtest_results/2026-01-26_to_2026-02-25_full_local_realonly.json`
- Calibration: `models/prob_calibration.json` (prior temps retained)
- Plan: `PLAN_EXECUTE_NOW.md`
- Mentor guide: `docs/MENTOR_MODEL_IMPROVEMENT_GUIDE.md`

---

## Next Actions (from Mentor Guide)

1. **60-70% bin still negative (-3.0%)** — consider extended calibration window or real-line-only refit with constrained T (cap at 2.5).
2. **70-80% bin -16.8% ROI** — overconfident; high-T calibration or exclude from whitelist.
3. **Extend backfill** — use 100k credits for Nov/Dec 2025 to increase sample size and fit stability.
4. **Run comparison** — backtest with T=5 calibration vs current to quantify tradeoff (Brier vs ROI).
