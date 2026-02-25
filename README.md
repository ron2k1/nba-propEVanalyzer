# NBA Prop Engine UI

Frontend + local API wrapper for `nba_mod.py`.

## What this adds

- `server.py`: local HTTP server that:
  - serves the UI from `web/`
  - wraps `nba_mod.py` commands as API endpoints
- `web/index.html`, `web/app.js`, `web/styles.css`: browser UI for:
  - slate view (`/api/games`)
  - prop EV runs (`/api/prop_ev`)
  - parlay EV runs (`/api/parlay_ev`)
  - tracking panel for best EV / settlement / yesterday results
- Modularized pipeline code:
  - `nba_data_collection.py` for API fetches, cache, and roster/games/player/team data
  - `nba_data_prep.py` (facade):
    - `nba_prep_projection.py` for projection feature engineering
    - `nba_prep_usage.py` for usage-adjustment logic
  - `nba_model_training.py` (facade):
    - `nba_ev_engine.py` for EV and odds math
    - `nba_prop_engine.py` for prop EV + auto sweep
    - `nba_parlay_engine.py` for parlay correlation EV
    - `nba_model_ml_training.py` for ML training/gating helpers
  - `nba_backtest.py` for historical calibration/ROI backtests
  - `nba_mod.py` now acts as a thin CLI dispatcher only

## Architecture for future additions

- Add new raw endpoints or cache strategies in `nba_data_collection.py`.
- Add new features/adjustments used by projections in `nba_data_prep.py`.
- Add pricing math, training/calibration routines, or betting logic in `nba_model_training.py`.
- Only add command wiring in `nba_mod.py` after logic exists in the right module.

## Requirements

- Python 3.10+ (you are currently on Python 3.14)
- Create an isolated virtual environment:

```powershell
cd "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

- Optional for sportsbook odds features:
  - Free key from The Odds API (`https://the-odds-api.com/`)
  - Set environment variable before starting app:

```powershell
$env:ODDS_API_KEY = "YOUR_KEY_HERE"
```

- Optional for injury/news ingestion (Option 3):
  - Free key from NewsAPI (`https://newsapi.org/`)
  - Set environment variable:

```powershell
$env:NEWS_API_KEY = "YOUR_NEWS_API_KEY"
```

- Optional for Option 1 ML projection training:
  - `scikit-learn` is required for `train_projection_ml`.
  - Install when needed:

```powershell
.\.venv\Scripts\python.exe -m pip install scikit-learn
```

If you use `run_ui.ps1` (which auto-elevates), pass the key explicitly so the elevated process keeps it:

```powershell
powershell -ExecutionPolicy Bypass -File ".\run_ui.ps1" -OddsApiKey "YOUR_KEY_HERE"
```

## Run

```powershell
cd "C:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\.venv\Scripts\python.exe .\server.py
```

Open:

- `http://127.0.0.1:8787`

## Optional launcher

Use:

```powershell
powershell -ExecutionPolicy Bypass -File ".\run_ui.ps1"
```

## Jupyter workflow (optional, recommended)

Useful for long-term model diagnostics, backtest experiments, and feature analysis.

Install into project venv:

```powershell
.\.venv\Scripts\python.exe -m pip install jupyterlab ipykernel
.\.venv\Scripts\python.exe -m ipykernel install --user --name nba-prop-engine --display-name "NBA Prop Engine"
```

Run Jupyter Lab from project:

```powershell
.\.venv\Scripts\python.exe -m jupyter lab
```

Then select kernel: `NBA Prop Engine`.

## API endpoints

- `GET /api/health`
- `GET /api/games`
- `GET /api/teams`
- `GET /api/players`
- `GET /api/player_lookup?q=anthony%20edwards&limit=10`
- `GET /api/team_players?teamIds=1610612747,1610612753`
- `POST /api/prop_ev`
- `POST /api/prop_ev_ml`
- `POST /api/auto_sweep`
- `POST /api/parlay_ev`
- `GET /api/odds?regions=us&markets=h2h,spreads,totals&bookmakers=draftkings,fanduel&sport=basketball_nba`
- `GET /api/odds_live?regions=us&markets=h2h,spreads,totals&bookmakers=draftkings,fanduel&sport=basketball_nba&maxEvents=8`
- `GET /api/best_today?limit=15`
- `GET /api/results_yesterday?limit=50`
- `GET /api/settle_yesterday?date=2026-02-24` (date optional; default yesterday)
- `GET /api/injury_news?team=LAL&lookbackHours=24`
- `GET /api/usage_adjust_news?player=LeBron%20James&team=LAL&lookbackHours=24`

## CLI odds commands

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" odds us "h2h,spreads,totals" "draftkings,fanduel" basketball_nba
.\.venv\Scripts\python.exe ".\nba_mod.py" odds_live us "h2h,spreads,totals" "draftkings,fanduel" basketball_nba 8
```

For player props, use `auto_sweep` (event-level prop fetch), not `odds/odds_live`.

## CLI player lookup and name support

You can use either `player_id` or `player_name` for:

- `player_log`
- `player_splits`
- `player_position`
- `projection`
- `prop_ev`
- `usage_adjust`

Quick lookup examples:

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" player_lookup "anthony edwards" 10
.\.venv\Scripts\python.exe ".\nba_mod.py" prop_ev "Anthony Edwards" ORL 1 pts 25.5 -110 -110 0 MIN
.\.venv\Scripts\python.exe ".\nba_mod.py" prop_ev_ml "Anthony Edwards" ORL 1 pts 25.5 -110 -110 0
.\.venv\Scripts\python.exe ".\nba_mod.py" auto_sweep "Anthony Edwards" MIN ORL 1 pts 0 us "draftkings,fanduel" basketball_nba 15
```

`auto_sweep` usage:

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" auto_sweep <player_id_or_name> <player_team_abbr> <opponent_abbr> <is_home:0|1> <stat> [is_b2b:0|1] [regions] [bookmakers_csv] [sport] [top_n]
```

## Option 1 (ML projection) commands

Train candidate model with time-based holdout:

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" train_projection_ml ".\examples\training_from_journal.csv" actual auto 0.2 50 gradient_boosting pickDate ".\models\candidate_projection_ml.pkl"
```

Promote candidate only if holdout gate passes (rmse+mae improvement thresholds):

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" promote_projection_ml ".\models\candidate_projection_ml.pkl" ".\models\production_projection_model.pkl" 1.0 1.0 0
```

Run prop EV with production ML override:

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" prop_ev_ml "Anthony Edwards" ORL 1 pts 25.5 -110 -110 0
```

## Option 3 (injury/news ingestion) commands

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" injury_news LAL 24
.\.venv\Scripts\python.exe ".\nba_mod.py" usage_adjust_news "LeBron James" LAL 24
```

## CLI tracking commands

Auto-logging is enabled for every successful `prop_ev` run.
Entries are stored in:

- `data/prop_journal.jsonl`

Daily workflow commands:

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" best_today 15
.\.venv\Scripts\python.exe ".\nba_mod.py" settle_yesterday
.\.venv\Scripts\python.exe ".\nba_mod.py" results_yesterday 50
.\.venv\Scripts\python.exe ".\nba_mod.py" export_training_rows ".\examples\training_from_journal.csv" csv
.\.venv\Scripts\python.exe ".\nba_mod.py" record_closing 2026-02-24 "[{\"entryId\":\"<ENTRY_ID>\",\"closingLine\":25.5,\"closingOdds\":-105}]"
```

UI equivalent:

- Open the **Tracking & Settlement** panel.
- `Load Best EV` = top edges for today (or selected date).
- `Settle Date` = settle pending picks for yesterday (or selected date).
- `Load Results` = view win/loss/push, hit rate, and PnL summary.

Auto line sweep:

- In **Prop EV** panel, fill player/team/opponent/stat.
- Click **Auto Sweep Best Line**.
- It scans available sportsbook lines for that player prop and ranks by EV.

Date override examples:

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" settle_yesterday 2026-02-24
.\.venv\Scripts\python.exe ".\nba_mod.py" results_yesterday 50 2026-02-24
.\.venv\Scripts\python.exe ".\nba_mod.py" export_training_rows ".\examples\training_2026_02.csv" csv 2026-02-01 2026-02-28
.\.venv\Scripts\python.exe ".\nba_mod.py" record_closing 2026-02-24 "[{\"playerId\":2544,\"stat\":\"pts\",\"line\":25.0,\"recommendedSide\":\"over\",\"closingLine\":26.5,\"closingOdds\":-120}]"
```

## CLI backtest commands

Single day:

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" backtest 2025-01-15
```

Date range:

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" backtest 2025-01-01 2025-01-31
```

Model variant selection:

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" backtest 2025-01-01 2025-01-31 --model full
.\.venv\Scripts\python.exe ".\nba_mod.py" backtest 2025-01-01 2025-01-31 --model simple
.\.venv\Scripts\python.exe ".\nba_mod.py" backtest 2025-01-01 2025-01-31 --model both
```

Backtest output includes:

- calibration bins (predicted over-probability vs actual hit rate)
- MAE by stat
- Brier score by stat
- simulated ROI for betting synthetic +EV spots (threshold-gated)

## Projection/EV config notes

- `PROJECTION_CONFIG["min_edge_threshold"]` defaults to `0.03`.
- EV sides now include `meetsThreshold` and only classify as value when threshold is met.
- `compute_projection` supports:
  - `model_variant="full"` (default) or `model_variant="simple"`
  - `blend_with_line` (Vegas blend): `final = 0.7 * model + 0.3 * line`
- `prop_ev` now line-shops same-line over/under prices when `player_team_abbr` is provided, and returns:
  - `bestOverOdds`, `bestUnderOdds`, `bestOverBook`, `bestUnderBook`

## CLI model training

Train a ridge calibrator model from historical rows:

```powershell
.\.venv\Scripts\python.exe ".\nba_mod.py" train_model ".\examples\calibration_sample.csv" actual auto 0.5 ".\models\calibration_sample_model.json"
```

Arguments:

- `data_path`: input file (`.csv`, `.json`, or `.jsonl`)
- `target_key`: target column/key (default `actual`)
- `feature_keys_csv|auto`: comma-separated feature keys or `auto` for numeric inference
- `ridge_alpha`: ridge regularization (default `0.5`)
- `output_model_path`: where to write model JSON (default `<data_path>_ridge_model.json`)

Supported training file shapes:

- CSV with headers (one row per example)
- JSON array of objects
- JSON object with `rows` array
- JSONL (one object per line)

Example training file:

- [examples/calibration_sample.csv](C:/Users/thegr/OneDrive/Desktop/nba%20data%20ver%202/examples/calibration_sample.csv)

### `POST /api/prop_ev` body

```json
{
  "playerId": 2544,
  "playerName": "LeBron James",
  "playerTeamAbbr": "LAL",
  "opponentAbbr": "ORL",
  "isHome": true,
  "isB2b": false,
  "stat": "pts",
  "line": 25.0,
  "overOdds": -110,
  "underOdds": -110
}
```

`playerId` or `playerName` is required. If both are provided, `playerId` is used.

### `POST /api/auto_sweep` body

```json
{
  "playerId": 1630162,
  "playerName": "Anthony Edwards",
  "playerTeamAbbr": "MIN",
  "opponentAbbr": "ORL",
  "isHome": true,
  "isB2b": false,
  "stat": "pts",
  "regions": "us",
  "bookmakers": "draftkings,fanduel",
  "sport": "basketball_nba",
  "topN": 15
}
```

### `POST /api/parlay_ev` body

```json
{
  "legs": [
    {
      "playerId": 2544,
      "playerTeam": "LAL",
      "stat": "pts",
      "line": 25.5,
      "side": "over",
      "probOver": 0.58,
      "overOdds": -110,
      "underOdds": -110
    },
    {
      "playerId": 2544,
      "playerTeam": "LAL",
      "stat": "reb",
      "line": 8.5,
      "side": "under",
      "probOver": 0.52,
      "overOdds": -115,
      "underOdds": -105
    }
  ]
}
```

## Notes

- If live calls to `stats.nba.com` fail with `WinError 10013`, run from a shell/profile with network permission.
- The API wrapper returns `nba_mod.py` output directly so existing response keys remain intact.
