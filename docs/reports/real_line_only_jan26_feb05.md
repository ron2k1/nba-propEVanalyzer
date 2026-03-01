# Real-Line-Only Performance Report — 2026-01-26 to 2026-02-05
> Generated from backtest with `oddsSource=local_history`
## 1. Coverage Confirmation
| Metric | Value |
|--------|-------|
| oddsSource | `local_history` |
| realLineSamples | 4,436 |
| missingLineSamples | 10,084 |
| coverage | 30.6% |
## 2. Segment Comparison
| Segment | Bets | Wins | Losses | Pushes | Hit% | ROI/bet |
|---------|------|------|--------|--------|------|---------|
| **Real line** | 1,936 | 996 | 940 | 0 | 51.4% | -2.5% |
| Synthetic line | 7,269 | 4,879 | 2,390 | 0 | 67.1% | +28.1% |
| **Blended** | 9,205 | 5,875 | 3,330 | 0 | 63.8% | +21.7% |
## 3. Per-Stat Real-Line Coverage
| Stat | Bets | Wins | Losses | Hit% | ROI/bet | Coverage |
|------|------|------|--------|------|---------|----------|
| pts | 516 | 268 | 248 | 51.9% | -2.2% | ok |
| reb | 455 | 223 | 232 | 49.0% | -7.8% | ok |
| ast | 460 | 240 | 220 | 52.2% | +1.2% | ok |
| fg3m | 0 | 0 | 0 | — | — | no coverage |
| pra | 505 | 265 | 240 | 52.5% | -1.4% | ok |
| stl | 0 | 0 | 0 | — | — | no coverage |
| blk | 0 | 0 | 0 | — | — | no coverage |
| tov | 0 | 0 | 0 | — | — | no coverage |

> **Stats with no real-line bets:** fg3m, stl, blk, tov  
> These stats had closing lines in the DB but no EV-positive bets qualified, or the market isn't offered by Odds API.
## 4. Confidence Bins (real-line bets only)
| Prob bin | Bets | Wins | Hit% | ROI/bet |
|----------|------|------|------|---------|
| 0-10% | 30 | 26 | 86.7% | +60.5% |
| 10-20% | 52 | 28 | 53.9% | -0.6% |
| 20-30% | 138 | 75 | 54.4% | -3.4% |
| 30-40% | 544 | 309 | 56.8% | +4.1% |
| 40-50% | 523 | 240 | 45.9% | -9.7% |
| 50-60% | 396 | 186 | 47.0% | -4.3% |
| 60-70% | 220 | 111 | 50.5% | -8.0% |
| 70-80% | 29 | 18 | 62.1% | +2.5% |
| 80-90% | 3 | 2 | 66.7% | +7.7% |
| 90-100% | 1 | 1 | 100.0% | +42.6% |

*Bins with zero real-line bets omitted.*
## 5. Key Findings
**Top stats by real-line ROI (≥5 bets):**
- **AST**: 460 bets, 52.2% hit rate, +1.2% ROI
- **PRA**: 505 bets, 52.5% hit rate, -1.4% ROI
- **PTS**: 516 bets, 51.9% hit rate, -2.2% ROI
- **REB**: 455 bets, 49.0% hit rate, -7.8% ROI

**Real vs synthetic gap:** -30.6 pp  
(Synthetic lines outperforming — investigate calibration)

**Missing market coverage:** fg3m, stl, blk, tov — Odds API does not offer player_turnovers or these markets weren't backfilled. Synthetic lines were used; exclude from real-money conclusions.
## 6. Final Verdict
**❌ NEGATIVE** — Real-line ROI is negative (-2.5%) despite positive blended ROI (+21.7%). Synthetic lines may be overstating edge.

**Risks:**
1. Coverage is only ~30% of all samples (4,436 / 14,520) — real-line segment may not be fully representative.
2. stl, blk, fg3m, tov have no Odds API market or sparse coverage — those stats fall back to synthetic lines regardless of `--odds-source`.
3. 11-day window (Jan 26 – Feb 5) is too narrow for statistical significance; expand backfill to confirm.

**Next actions:**
1. Run `backfill_odds_history.py` to extend coverage through Feb 25, then rerun this analysis on the full 30-day window.
2. If real-line ROI remains positive, prioritize pts/reb/ast/pra bets where `clvLine > 0` AND `clvOddsPct > 0` as primary GO signals.
3. Add minimum 20-bet threshold before drawing per-stat conclusions; current sample too small for stl/blk/fg3m.
