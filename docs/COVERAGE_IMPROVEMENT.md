# Raising Real-Line Coverage for Backtests

**Goal:** `realLineSamples / sampleCount >= 70%` (see `docs/PLAN_CLAUDE_CODE.md` Phase 1 and Step 2).

At **26% coverage** (e.g. 1,697 / 33,928), most backtest samples use synthetic lines; real-line ROI is the only decision signal, so we need more historical closing lines.

## Why coverage is low

- **Backtest** needs a closing line per (game, player, stat): it looks up `event_id` via `find_event_for_game(home, away, date)` (from the **snapshots** table), then `get_closing_line(event_id, market, player_name)` from the **closing_lines** table.
- **closing_lines** are derived from **snapshots** by `odds_build_closes` (last snapshot per event × book × market × player before game start).
- If you never ran a historical **odds_backfill** for the backtest window (or only for some dates), you have few or no snapshots for those games → no or few closing lines → low real-line coverage.
- **Player coverage:** Books only list props for rotation players. Bench players often have no line, so 100% is impossible; 50–70%+ is realistic for the stats you backfill.

## Steps to increase coverage

### 1. See current state

```powershell
.\.venv\Scripts\python.exe nba_mod.py odds_coverage --by-date 2026-01-26 2026-02-25
```

Check `coverageByDate`: which dates have low or zero `closingRows` / `events`. Those are the gaps to backfill.

### 2. Backfill the backtest window

Use the same **books** and **stats** the backtest cares about. BETTING_POLICY whitelist is `pts`, `ast`, `pra` (reb removed). Backfill at least those so real-line counts include pra:

```powershell
# Full window in one go (higher API usage)
.\.venv\Scripts\python.exe nba_mod.py odds_backfill 2026-01-26 2026-02-25 --books betmgm,draftkings,fanduel --stats pts,ast,pra --offset-minutes 60 --max-requests 90000 --resume

# Or in 5–7 day chunks to cap credits (~19.5k per 1950 requests)
.\.venv\Scripts\python.exe nba_mod.py odds_backfill 2026-01-26 2026-02-01 --books betmgm,draftkings,fanduel --stats pts,ast,pra --offset-minutes 60 --max-requests 1950 --resume
.\.venv\Scripts\python.exe nba_mod.py odds_backfill 2026-02-02 2026-02-08 --books betmgm,draftkings,fanduel --stats pts,ast,pra --offset-minutes 60 --max-requests 1950 --resume
# ... repeat for remaining weeks
```

- `--resume` skips dates that already have snapshots (safe to re-run).
- After Phase 2 snap-offsets, you can add `--snap-offsets -10,-120,-240` to capture true closing (-10 min) and mid-move snapshots; then re-run `odds_build_closes` so closing line uses -10 min.

### 3. Rebuild closing lines

After each backfill chunk (or once at the end):

```powershell
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes 2026-01-26 2026-02-25
```

### 4. Re-run backtest

```powershell
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-01-26 2026-02-25 --model full --local --odds-source local_history --save
```

Check the report: `realLineSamples`, `missingLineSamples`, and **coverage %** = `realLineSamples / (realLineSamples + missingLineSamples)`.

**Pass:** coverage ≥ 70% on this backtest.

## Optional: include reb in backfill

If you want real-line metrics for reb (e.g. for analysis even though reb is excluded from BETTING_POLICY), add `reb` to `--stats` in the backfill commands above. Same books; one more market per event.

## Summary

| Step | Command / action |
|------|-------------------|
| 1. Check gaps | `nba_mod.py odds_coverage --by-date <from> <to>` |
| 2. Backfill | `nba_mod.py odds_backfill <from> <to> --books betmgm,draftkings,fanduel --stats pts,ast,pra --offset-minutes 60 [--max-requests N] --resume` |
| 3. Build closes | `nba_mod.py odds_build_closes <from> <to>` |
| 4. Verify | `nba_mod.py backtest <from> <to> --model full --local --odds-source local_history` → check realLineSamples and coverage % |

Yes — to fix low coverage you need to **add more data** (backfill) and **derive closes** (build_closes). The pipeline is already in place; running it for the full backtest window is what raises coverage toward 70%.
