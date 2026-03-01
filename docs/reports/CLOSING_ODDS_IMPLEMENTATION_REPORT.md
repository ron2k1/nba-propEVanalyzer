# Closing Odds Strategy — Implementation Report

> Real-world historical closing-odds pipeline with minimal API cost and explicit fallbacks.

---

## 1. What Was Implemented

### A. Zero-Cost LineStore → OddsStore Bridge

| Component | Path | Purpose |
|-----------|------|---------|
| Bridge script | `scripts/line_to_odds_bridge.py` | Copies LineStore JSONL snapshots into OddsStore SQLite |
| CLI command | `nba_mod.py line_bridge [date_from] [date_to]` | Run bridge for a date range |
| Schema mapping | LineStore (game_id, stat, line, over/under_odds) → OddsStore (event_id, market, side, line, odds) | Normalized for `build_closing_lines` |

**Idempotency:** `INSERT OR IGNORE`; exact duplicates skipped. Safe to run repeatedly.

**Options:** `--dry-run`, `--books`, `--stats`, `--line-dir`, `--db`

### B. Coverage Enhancements

| Change | Location | Details |
|--------|----------|---------|
| `coverage_by_date()` | `core/nba_odds_store.py` | Per-date closing row count (realLineSamples potential) |
| `odds_coverage --by-date` | `nba_cli/line_commands.py` | `--by-date date_from date_to` returns `coverageByDate`, `totalClosingInRange` |

### C. Operational Documentation

| Doc | Path | Content |
|-----|------|---------|
| Strategy & workflow | `docs/reports/CLOSING_ODDS_STRATEGY.md` | Multi-source options, scraping risk table, daily schedule |
| CLAUDE.md update | `CLAUDE.md` | Zero-cost bridge workflow, roiReal-primary rule |

### D. Validation

| Component | Path | Purpose |
|-----------|------|---------|
| Bridge smoke test | `scripts/validate_line_bridge.py` | Dry-run, real run with temp data, `build_closing_lines` |
| Quality gate | `scripts/quality_gate.py` | `--full` runs `validate_line_bridge` |

---

## 2. What Is Reliable Now

- **Bridge:** Converts LineStore schema to OddsStore; dedupes; populates home_team/away_team for `find_event_for_game`.
- **build_closing_lines:** Works with both API backfill and bridged snapshots; no changes needed.
- **odds_coverage --by-date:** Per-date `closingRows` and `events`; useful to estimate real-line potential before backtest.
- **roiReal vs roiSynth:** Backtest already reports both; `roiReal` promoted as primary decision metric in docs.

---

## 3. What Remains Risky

- **Scraping:** OddsPortal, sbr-odds-scraper, pysbr — ToS risk, maintenance burden, block risk. See strategy doc risk table. Not recommended without legal review.
- **Alternative APIs:** Prop Odds, SportsGameOdds — require new adapters; cost/coverage TBD.
- **Historical gaps:** Bridge only helps from the day you start running `collect_lines`. Old dates still need Odds API backfill or purchased data.

---

## 4. Recommended Production Path

1. **Routine:** `collect_lines` 2–3× daily; end-of-day `line_bridge` + `odds_build_closes`.
2. **Backtest:** Use `--odds-source local_history` when coverage exists; report `roiReal` as primary.
3. **API quota:** If exhausted, evaluate Prop Odds API or SportsGameOdds before scraping.
4. **Historical:** Backfill via Odds API when quota allows; use `--resume` to skip existing dates.

---

## 5. Usage Examples

```powershell
# Bridge LineStore → OddsStore (zero API cost)
.\.venv\Scripts\python.exe nba_mod.py line_bridge 2026-02-20 2026-02-25
.\.venv\Scripts\python.exe nba_mod.py line_bridge 2026-02-27 --dry-run

# Coverage with per-date breakdown
.\.venv\Scripts\python.exe nba_mod.py odds_coverage
.\.venv\Scripts\python.exe nba_mod.py odds_coverage --by-date 2026-02-01 2026-02-27

# Build closes, then backtest
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-02-01 2026-02-25 --model full --local --odds-source local_history --save

# Validation
.\.venv\Scripts\python.exe scripts\validate_line_bridge.py
```

---

## 6. Backtest Output Fields (roiReal / roiSynth)

When running with `--odds-source local_history`:

- `realLineSamples` — player-stats that used real closing lines
- `missingLineSamples` — fell back to synthetic lines (coverage gap)
- `roiReal` — `{betsPlaced, wins, losses, pnlUnits, roiPctPerBet, hitRatePct}`
- `roiSynth` — same structure for synthetic-line bets

**Rule:** Use `roiReal` for GO/NO-GO. Treat `roiSynth` as diagnostic only.
