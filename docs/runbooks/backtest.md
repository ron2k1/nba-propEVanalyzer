# Backtest Runbook

## No-Lookahead Rule

**`date_to` must be strictly before today.** Never feed future outcomes to the model.

## Key Commands

```powershell
# Full backtest — zero API calls
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-01-26 2026-02-25 --model full --local --save

# With real closing lines
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-01-26 2026-02-25 --model full --local --odds-source local_history --save

# Minutes model evaluation
.\.venv\Scripts\python.exe nba_mod.py minutes_eval 2026-01-26 2026-02-25 --local

# Fit calibration from backtest
.\.venv\Scripts\python.exe scripts\fit_calibration.py --input data/backtest_results/<file>.json --output models/prob_calibration.json

# 60-day rolling backtest (run weekly, auto-logs to data/backtest_60d_log.jsonl)
.\.venv\Scripts\python.exe nba_mod.py backtest_60d           # date_to = yesterday
.\.venv\Scripts\python.exe nba_mod.py backtest_60d 2026-02-27  # explicit date_to
```

## Local Mode Tips

- `--local` falls back to NBA API if `local_provider.max_date < end_date`
- Always set backtest end date within local index coverage (max_date=2026-02-25)
- Local mode runs ~50x faster (no API delays)

## Real-Line vs Synthetic-Line

- **Real-line ROI** (`roiReal`): uses actual closing lines from odds history. This drives decisions.
- **Synthetic ROI** (`roiSynth`): uses model-generated lines. Calibration diagnostic only, not a real-money estimate.
- Always report both, clearly labeled. Never mix them.

## Calibration Fit Workflow

1. Run backtest with `--save`
2. Run `fit_calibration.py --input <result.json> --output models/prob_calibration.json`
3. Document Brier scores pre/post
4. Verify all 8 stats show Brier improvement vs uncalibrated
5. Check `_fitted_at` in the output JSON

## Tier 2: 2024-25 Season (Fixed)

`get_team_defensive_ratings` and `get_position_vs_team` now derive the correct NBA season string from `as_of_date` via `_season_for_date()`. A March 2025 date resolves to "2024-25", preventing lookahead contamination from 2025-26 data. `get_player_position` uses `CommonPlayerInfo` which is season-agnostic and was never affected. When `as_of_date` is None, all functions still default to `CURRENT_SEASON` (backwards compatible).
