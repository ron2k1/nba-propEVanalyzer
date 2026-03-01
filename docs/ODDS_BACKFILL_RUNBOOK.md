# Odds API Backfill Runbook — Use Tokens to Get All Historical Odds

Backfill historical pregame/closing odds from the Odds API into the local SQLite store, then build closing lines so backtests get maximum real-line coverage.

---

## 1. One terminal (sequential)

Runs all date chunks one after another in a single PowerShell window. Uses `--resume` so re-runs skip dates already in the DB.

```powershell
cd "c:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\scripts\run_odds_backfill_chunks.ps1
```

**Default:** `2025-10-21` → `2026-02-28`, 14-day chunks, 2,500 API requests per chunk, then `odds_build_closes` once at the end.

**Custom range / chunk size:**

```powershell
.\scripts\run_odds_backfill_chunks.ps1 -DateFrom "2025-10-21" -DateTo "2026-02-28" -ChunkDays 7 -MaxRequestsPerChunk 2000
```

- **ChunkDays** — Number of days per chunk; smaller = more chunks, more control over `--max-requests` per run.
- **MaxRequestsPerChunk** — Cap API calls per chunk (~10 credits per request). Example: 2,500 requests ≈ 25k credits per chunk.

---

## 2. Multiple terminals (parallel)

Use your tokens faster by running several chunks in parallel in **separate** PowerShell windows. Each window runs a different date range; all use the same DB and `--resume`.

**Step 1 — Print the commands (no API calls):**

```powershell
cd "c:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\scripts\run_odds_backfill_chunks.ps1 -PrintOnly
```

**Step 2 — Copy each printed command into its own PowerShell window** (same working directory). Run them at the same time. Example output:

```
# Chunk 1 of 10 (2025-10-21 -> 2025-11-03)
Set-Location "c:\...\nba data ver 2"; .\.venv\Scripts\python.exe nba_mod.py odds_backfill 2025-10-21 2025-11-03 --books ... --resume

# Chunk 2 of 10 (2025-11-04 -> 2025-11-17)
...
```

**Step 3 — After all chunks finish**, in any one window run:

```powershell
Set-Location "c:\Users\thegr\OneDrive\Desktop\nba data ver 2"
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes 2025-10-21 2026-02-28
```

**Note:** Multiple processes writing to the same SQLite DB can occasionally hit `SQLITE_BUSY`. If a chunk errors with a DB lock, re-run that chunk alone (it will `--resume` and skip completed dates).

---

## 3. Verify

```powershell
.\.venv\Scripts\python.exe nba_mod.py odds_coverage
.\.venv\Scripts\python.exe nba_mod.py odds_coverage --by-date 2025-10-21 2026-02-28
```

Then run a backtest with `--odds-source local_history` and check `realLineSamples` and coverage %.

---

## 4. Token usage (rough)

- ~10 credits per API call (per event×stat×book snapshot).
- One date with 10 games, 3 stats, 3 books → ~900+ calls/day (discover + events×stats).
- **MaxRequestsPerChunk 2500** ≈ 2–3 weeks of dates per chunk, ~25k credits per chunk.
- Full season (Oct–Feb) in 14-day chunks ≈ 10 chunks → ~250k credits if no resume; with `--resume`, only missing dates are fetched.

Use `-MaxRequestsPerChunk` to cap credits per run; run multiple chunks (sequential or parallel) until the range is filled or quota is used.
