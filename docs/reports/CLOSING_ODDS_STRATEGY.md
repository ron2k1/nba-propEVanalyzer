# Multi-Source Historical Closing Odds Strategy

> Real-world backtest validity: maximize real closing-line coverage, minimize API cost, explicit fallbacks.

## 1. Decision Rule: roiReal Primary, Synthetic Diagnostic

- **Primary metric:** `roiReal` (ROI on bets that used real closing lines). Use for GO/NO-GO decisions.
- **Secondary metric:** `roiSynth` (synthetic ±0.5 lines). Diagnostic only — do not treat as real-money estimate.
- **Coverage gate:** If `realLineSamples=0` for a date range, backtest P&L is not real-world-valid.

---

## 2. Multi-Source Options

### A. Paid API Historical Backfill (Existing)

- **Pipeline:** `backfill_odds_history.py` → OddsStore snapshots → `build_closing_lines.py` → closing_lines.
- **Cost:** 10 credits per event per market per region (Odds API). ~500+ credits per day for 5 stats.
- **Coverage reporting:** `nba_mod.py odds_coverage` and `odds_coverage --by-date 2026-02-01 2026-02-25`.
- **When to use:** When you have API quota and need historical gaps filled.

### B. Alternative Datasets (CSV/Parquet/SQLite)

| Source | Format | Legality | Schema Mapping | Status |
|--------|--------|----------|----------------|--------|
| Prop Odds API | JSON/API | Commercial ToS | New adapter needed | Evaluate if API quota exhausted |
| SportsGameOdds | JSON/API | Commercial ToS | New adapter needed | Evaluate pricing |
| RapidAPI Historical | JSON | Commercial ToS | OddsPortal-style; player props unclear | Verify player prop coverage |
| Local Parquet | Parquet | User-owned | `scripts/stage_local_parquet.py` | Add loader if you obtain bulk export |

**Recommendation:** If adding external datasets, implement a normalized loader that writes into OddsStore `snapshots` schema; then `build_closing_lines` works unchanged.

### C. Scraping / DIY — Risk Assessment

| Option | ToS Risk | Maintenance | Data Quality | Block Risk | NBA Feasibility |
|--------|----------|-------------|--------------|------------|-----------------|
| OddsPortal | High (no official API) | High | Good | Medium | Possible but unsupported |
| sbr-odds-scraper (PyPI) | Medium | Medium | MLB-focused | Low | NBA: assess viability |
| pysbr | Medium | Medium | SBR data | Low | Assess for NBA closing |
| Custom scraper | High | Very high | Variable | High | Possible |

**Compliance:** Any scraper must include a ToS/compliance warning and a `--disable-scraper` or env toggle. Deterministic parsing logic stays in Python; LLM (e.g. local Ollama) may assist extraction only as an optional helper, not source of truth.

### D. Going-Forward Zero-Cost Collection (Implemented)

- **Bridge:** `line_bridge` copies LineStore JSONL → OddsStore snapshots.
- **Cost:** $0 extra API. Uses existing `collect_lines` data.
- **Process:** Run `collect_lines` 2–3× daily before tipoff; end-of-day `line_bridge` + `odds_build_closes`.
- **Limitation:** No historical coverage for dates before you started collecting. Backfill or alternatives needed for past dates.

---

## 3. Operational Workflow

### Daily Schedule (Pre-Game)

1. **11:00 ET** — `collect_lines` (capture morning lines)
2. **14:00 ET** — `collect_lines` (midday)
3. **17:00 ET** — `collect_lines` (near tipoff; last best closing proxy)

### End-of-Day (After Games Complete)

1. `line_bridge YYYY-MM-DD YYYY-MM-DD`
2. `odds_build_closes` (or with `--date-from` / `--date-to` if scoped)

### Weekly Real-Line Backtest

1. `odds_coverage --by-date 2026-02-01 2026-02-27` — verify coverage
2. `backtest 2026-02-01 2026-02-27 --model full --local --odds-source local_history --save`
3. Inspect `realLineSamples`, `roiReal`, `roiSynth` in saved JSON

---

## 4. Commands Summary

```powershell
# Zero-cost bridge (LineStore → OddsStore)
.\.venv\Scripts\python.exe nba_mod.py line_bridge 2026-02-20 2026-02-25
.\.venv\Scripts\python.exe nba_mod.py line_bridge 2026-02-27 --dry-run --books betmgm,draftkings --stats pts,reb,ast

# Coverage with per-date breakdown
.\.venv\Scripts\python.exe nba_mod.py odds_coverage
.\.venv\Scripts\python.exe nba_mod.py odds_coverage --by-date 2026-02-01 2026-02-27

# Backtest with real lines
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-02-01 2026-02-25 --model full --local --odds-source local_history --save
```

---

## 5. What Is Reliable Now

- **LineStore → OddsStore bridge:** Idempotent, schema-mapped, dedupe via INSERT OR IGNORE.
- **odds_coverage --by-date:** Per-date closing row counts (realLineSamples potential).
- **build_closing_lines:** Works with both API backfill and bridged snapshots.
- **roiReal/roiSynth split:** Backtest already reports both; use roiReal for decisions.

---

## 6. What Remains Risky

- **Scraping:** All scraping options carry ToS and maintenance risk. Not recommended for production without legal review.
- **Alternative APIs:** Prop Odds, SportsGameOdds require new adapters and cost evaluation.
- **Historical gaps:** Bridge only helps from the day you start collecting. Old dates still need backfill or purchased data.

---

## 7. Recommended Production Path

1. **Routine:** Run `collect_lines` 2–3× daily; `line_bridge` + `odds_build_closes` end-of-day.
2. **Backtest:** Always use `--odds-source local_history` when coverage exists; report `roiReal` as primary.
3. **API quota:** If exhausted, evaluate Prop Odds API or SportsGameOdds before considering scraping.
4. **Historical:** Backfill via Odds API when quota allows; use `--resume` to skip existing dates.
