# Requirements

## GO-LIVE Gate (Primary)

The system must pass ALL of the following before live betting begins:

- [ ] `paper_summary → gate.gatePass: true`
- [ ] `sample >= 30` settled signals in 14-day window
- [ ] `roi > 0.0` over the window
- [ ] `positive_clv_pct >= 50.0`
- [ ] No single stat with ≥20 signals AND hit rate < 45%
- [ ] Earliest date: 2026-03-14
- [ ] Validation metrics exclude manual `book=user_supplied` entries

## Calibration Requirements

- [ ] Avg Brier < 0.235 (current: 0.233 — passes)
- [ ] All 8 stats show Brier improvement vs uncalibrated
- [ ] Minutes 35+ bucket bias < ±3.0 min (current: +5.8 min — OPEN)
- [ ] `models/prob_calibration.json` exists; `_fitted_at` < 60 days old
- [ ] blk/fg3m 60–80% bin gaps documented; NOT used as GO signals
- [ ] reb 60-70% bin gap < 12% (current: 9.2% post-cal — passes)

## Signal Quality Requirements

- Policy whitelist for GO signals: `{pts, ast}`
- Research-only signal tracking may still log `reb` if a real Odds API line exists
- Eligible signal stats (`SIGNAL_SPEC`): `{pts, reb, ast}`
- Edge ≥ 0.08 (`ast`: ≥ 0.09, `reb`: ≥ 0.08)
- Confidence ≥ 0.60
- Active probability bins: `0` and `9` only (`blocked_prob_bins = {1,2,3,4,5,6,7,8}`)
- reb signals: real Odds API line required (no synthetic fallback)
- CLV gate (Week 2-3): `clvLine > 0` AND `clvOddsPct > 0`

## Coverage Requirements

- Real-line coverage target: ≥ 70% (`realLineSamples / sampleCount`)
- Current: 26% — requires odds backfill (Days 1-3)
- After backfill: re-run 30d backtest to confirm coverage

## Code Quality Requirements

- `quality_gate.py --json` → `"ok": true` before every commit
- `prop_ev` smoke test → `"success": true` with `distributionMode`
- Every `compute_ev()` call includes `stat=<stat_key>`
- Import policy: `core/` relative; `nba_cli/`+`scripts/` absolute
- No `.env`, `.pkl`, or raw data in commits

## Weekly Monitoring Targets

| Metric | Target |
|--------|--------|
| Real-line hit rate | > 52.4% |
| Real-line ROI/bet | > 0% |
| CLV-positive % | ≥ 50% |
| pts/ast ROI each | > 0% |
| reb ROI | research-only; monitor separately |
| Bin 0 hit rate | monitor |
| Bin 9 hit rate | monitor (small sample until stabilized) |
| Minutes 35+ bias | < ±3.0 min (after Step 4) |
| Brier avg | < 0.235 |
