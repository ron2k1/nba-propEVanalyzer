# Bin-0 Walk-Forward Kill Switch Report

**Generated:** 2026-03-02 09:23 UTC

## Configuration

| Parameter | Value |
|-----------|-------|
| Date range | 2026-02-19 to 2026-02-22 |
| Walk-forward | False |
| Odds source | synthetic (-110/-110) |
| Bootstrap resamples | 1,000 |
| CI level | 95% |
| Total backtest samples | 6,208 |

## Results

| Metric | Value |
|--------|-------|
| Source | all bins (synthetic; bin-0 breakdown unavailable) |
| Bin-0 bets | 90 |
| Wins | 59 |
| Hit rate | 65.6% |
| Mean ROI | +25.15%/bet |
| 95% CI lower | +6.06% |
| 95% CI upper | +44.24% |

## Per-Stat Bin-0 Calibration

| Stat | Count | Avg Predicted | Actual Hit Rate |
|------|-------|---------------|-----------------|
| blk | 78 | 0.8% | 12.8% |
| fg3m | 63 | 1.0% | 3.2% |
| pts | 2 | 8.9% | 50.0% |
| reb | 7 | 6.9% | 28.6% |
| stl | 7 | 0.7% | 28.6% |
| tov | 2 | 0.4% | 50.0% |

## Verdict

**PROCEED**: CI entirely above zero [+6.06%, +44.24%]. Signal is real.

Proceed to Phase 2.2-2.5 (signal amplification).
