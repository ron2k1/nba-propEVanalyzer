# Bin-0 Walk-Forward Kill Switch Report

**Generated:** 2026-03-02 14:57 UTC

## Configuration

| Parameter | Value |
|-----------|-------|
| Date range | 2025-10-21 to 2026-02-25 |
| Walk-forward | True |
| Odds source | local_history |
| Bootstrap resamples | 10,000 |
| CI level | 95% |
| Total backtest samples | 151,648 |

## Results

| Metric | Value |
|--------|-------|
| Source | real closing lines |
| Bin-0 bets | 1,138 |
| Wins | 793 |
| Hit rate | 69.7% |
| Mean ROI | +33.03%/bet |
| 95% CI lower | +27.83% |
| 95% CI upper | +38.23% |

## Per-Stat Bin-0 Calibration

| Stat | Count | Avg Predicted | Actual Hit Rate |
|------|-------|---------------|-----------------|
| ast | 336 | 4.6% | 26.8% |
| blk | 5123 | 5.2% | 13.9% |
| fg3m | 2251 | 1.9% | 6.9% |
| pra | 252 | 4.0% | 29.8% |
| pts | 339 | 4.2% | 31.6% |
| reb | 313 | 4.2% | 29.4% |
| stl | 392 | 1.5% | 25.5% |
| tov | 173 | 2.0% | 32.4% |

## Verdict

**PROCEED**: CI entirely above zero [+27.83%, +38.23%]. Signal is real.

Proceed to Phase 2.2-2.5 (signal amplification).
