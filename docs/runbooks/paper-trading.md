# Paper Trading Runbook

## Daily Routine (Game Days)

### Before Games (2-3x: 11am, 2pm, 5pm ET)
```powershell
.\.venv\Scripts\python.exe nba_mod.py collect_lines --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra
.\.venv\Scripts\python.exe nba_mod.py best_today 20
```

### Pre-Tip (6:30 PM ET)
```powershell
.\.venv\Scripts\python.exe nba_mod.py top_picks 5
```

### End-of-Day
```powershell
.\.venv\Scripts\python.exe nba_mod.py line_bridge --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes
```

### Next Morning
```powershell
.\.venv\Scripts\python.exe nba_mod.py paper_settle <yesterday-date>
.\.venv\Scripts\python.exe nba_mod.py results_yesterday 50
.\.venv\Scripts\python.exe nba_mod.py paper_summary --window-days 14
```

## CLV Rule

`clvLine > 0` AND `clvOddsPct > 0` required for high-quality bets. Positive model EV alone is not sufficient.

## GO-LIVE Gate

`paper_summary` → `gate.gatePass` requires:
- `sample >= 50`
- `roi > 0.0`
- `positive_clv_pct >= 50.0`
- No stat with >= 20 signals AND hit rate < 45%

## Model Comparison Protocol

Same date range, same books/stats, same odds DB. Compare `roiReal`, `hitRate` on real-line subset, and `realLineSamples` count only.

## Signal Deduplication

Signals auto-logged on qualifying `prop_ev`/`auto_sweep` calls. Deduped by `(player_id, stat, book, line, date)`. Re-runs on same day produce `journalDuplicateSignal` — this is expected.
