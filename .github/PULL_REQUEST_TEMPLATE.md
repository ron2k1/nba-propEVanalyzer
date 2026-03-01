## Summary
<!-- What does this PR do? -->

## Quality Gate
- [ ] `quality_gate.py --json` → `"ok": true`
- [ ] `prop_ev "Anthony Edwards" ORL 1 pts 25.5 -110 -110 0 MIN` → `"success": true` with `distributionMode`
- [ ] Backtest smoke: `backtest 2026-02-19 2026-02-22 --model full --local` → `sampleCount > 0`, errors < 5%
- [ ] No `.env`, `data/`, or `*.pkl` in staged changes

## Type
- [ ] Bug fix
- [ ] Feature
- [ ] Calibration / threshold change
- [ ] Chore / cleanup

## Notes
<!-- Calibration changes: document pre/post Brier scores. Threshold changes: document ROI impact. -->
