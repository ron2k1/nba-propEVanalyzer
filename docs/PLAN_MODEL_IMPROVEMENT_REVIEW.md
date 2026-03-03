# Model Improvement Plan — Review: What’s Good & Improvements

## Latest backtest results (post 8.14/8.12/8.8 + refactor)

**Range:** 2026-01-26 → 2026-02-25 | **Flags:** `--model full --local --odds-source local_history --save`

| Metric | Value |
|--------|--------|
| realLineSamples | 8,090 |
| roiReal | 213 bets, 138W / 75L, **64.79% hit**, **+12.80% ROI**, 27.27 pnl units |
| roiSynth | 106 bets, 82W / 24L, 77.36% hit, +47.68% ROI |
| Policy | statWhitelist: pts, ast | blockedProbBins: 2,3,4,5,6 |

Use this as the **current baseline** for phase gates. Previous baseline (215 bets, 65.1% hit, +13.9% ROI) is superseded; new run shows slightly fewer bets, similar hit rate, similar ROI.

---

## New gap: Backtest JSON structure vs script assumptions

**Issue:** Inline or ad-hoc scripts that parse backtest JSON assumed `calibrationByStat[stat]` is a dict with a key `realLineCalibBins`. It is not.

- **`reports.full.realLineCalibBins`** — **list** of bin objects (0-10%, 10-20%, …) with `betsPlaced`, `wins`, `hitRatePct`, `roiPctPerBet`. Use this for real-line bin ROI/hit summary.
- **`reports.full.calibrationByStat`** — **dict** of stat → **list** of bin objects for calibration (each with `bin`, `count`, `avgPredOverProbPct`, `actualOverHitRatePct`). Used by `fit_calibration.py`; no `realLineCalibBins` key.

**Fix:** For real-line bin summary use `r["realLineCalibBins"]`. For per-stat calibration fitting use `r["calibrationByStat"][stat]` (list). Do not use `calib.get("pts", {}).get("realLineCalibBins")` — `calibrationByStat["pts"]` is a list. Prefer `scripts/backtest_summary.py` or document the schema in `docs/` so one-off parsers stay correct.

---

## What’s good

- **Clear target and baseline** — 65–70% real-line hit rate. Baseline updated above (213 bets, 64.79% hit, +12.80% ROI, bins 0–1 + 70–80% + 80–90% + 90–100%).

- **Gap list matches the codebase** — CLV gate really is unused at log time (`clvLine`/`clvOddsPct` only in settlement); `referenceBook` is on the result but not in `_qualifies()`; `props_scan` (offline_scan.py) does not call `log_prop_ev_entry` or `DecisionJournal.log_signal`; `recentHighVariance` is in projection output but not checked in `_qualifies()`. Plan correctly identifies these.

- **Phased and ordered** — Signal-quality hardening (P1) before volume (P2), then calibration (P3), then game total (P4). Each phase has a backtest/paper-trade gate. Subagent order (1a→1b→1c→2a→2b→3a→3b→4a→4b) is dependency-safe.

- **Backtest protocol** — Same range (Jan 26–Feb 25), same flags (`--model full --local --odds-source local_history`), and a comparison table for bets/roiReal/hitRate per phase. Calibration validation on a held-out window (Jan 10–25) is sound.

- **SIGNAL_SPEC as single source of truth** — Pinnacle thresholds and `block_high_variance` in spec keep behavior tunable without code edits.

- **fit_calibration.py reuse** — Script already has `--min-pred` / `--max-pred`; Phase 3a only needs `--max-pred 0.25` and a separate output path (e.g. `prob_calibration_bins01.json`). No new flags required.

- **LineStore already supports P1c** — `get_opening_line()` and `get_closing_line()` give first vs latest snapshot per (date, player, stat, book). “Intraday CLV” = opening vs current (latest) line; no need for a new `get_latest_snapshot` API if “current” is the latest snapshot in the file.

- **roster_sweep builds on existing pieces** — `offline_scan.py` already does: get_snapshots → dedupe → roster → compute_projection + compute_ev. Plan correctly says “wire to decision journal” (add `_qualifies` + `log_signal` / optionally `log_prop_ev_entry`) rather than redesign the scan.

- **Subagent roles** — PhD coder for implementation, rollingrock for backtests, ronbot for paper-trading scans is a sensible split.

---

## Improvements

### 1. _qualifies() API: use prop_result, not ctx

Plan says: “if require_pinnacle and **ctx**.get('referenceBook') is None”.

**Reality:** `_qualifies(prop_result, stat, used_real_line=None)` only receives `prop_result`. There is no separate `ctx`. Reference book is on the result.

**Change:** In the plan and implementation, use **prop_result.get("referenceBook")**. Same for `recentHighVariance`: use **prop_result.get("recentHighVariance")** (and ensure projection/output is merged into the dict passed to `_qualifies` where needed).

---

### 2. Where to store referenceBook: journal vs JSONL

Plan says: “Pass [referenceBook] through to **log_prop_ev_entry()** context kwarg so it gets stored in context_json.”

**Reality:**  
- `log_prop_ev_entry()` (nba_bet_tracking) writes to **JSONL** and has **no** `context` or `context_json` argument.  
- `DecisionJournal.log_signal(..., context=...)` writes to **SQLite** and stores `context` as `context_json`.

**Change:**  
- **Decision journal:** Pass `context={"referenceBook": result.get("referenceBook"), "recentHighVariance": result.get("recentHighVariance")}` (and any other P1/P2 fields) into **log_signal()** in ev_commands.py and wherever else we log signals. Do not mention “log_prop_ev_entry context” for referenceBook.  
- Optionally, in a separate task, extend `log_prop_ev_entry` to accept an optional `extra_meta` dict and persist it in the JSONL entry for consistency.

---

### 3. Pinnacle gate: bin 0 vs bin 1 and noVig direction

Plan: bin 0 → noVigUnder ≥ 0.75; bin 1 → noVigUnder ≥ 0.65.

**Clarify:**  
- For **under** recommendations we care about noVigUnder. For **over** recommendations we care about noVigOver. Either require the **recommended side’s** no-vig prob above the threshold, or define both (e.g. “if recommended_side == 'over' then noVigOver ≥ X else noVigUnder ≥ X”).  
- Specify behavior when **referenceBook is present but Pinnacle market is missing** for that stat (skip vs fail).

---

### 4. Per-stat Pinnacle thresholds (gap #7)

Plan lists “No per-stat Pinnacle threshold” as a gap but phases only add a single bin-based rule.

**Improvement:** Add to Phase 1a (or a small 1d): store in SIGNAL_SPEC e.g. `pinnacle_min_no_vig_by_stat: {"pts": 0.72, "ast": 0.65}` and in `_qualifies()` use the threshold for the current stat (fallback to 0.65/0.75 if not set). Then bin 0/1 rules can override or sit on top of that.

---

### 5. roster_sweep vs props_scan naming and reuse

Plan introduces **roster_sweep** as a new command that “calls compute_prop_ev() … if qualifies call log_prop_ev_entry()”.

**Reality:** `props_scan` already runs offline_scan.py, which does projection + EV for all LineStore lines but does **not** log to the journal. So we have two options:

- **A)** Add a “log to decision journal” path inside offline_scan.py (or the CLI that invokes it), and optionally keep the name `props_scan` with a flag like `--journal` to log qualifying signals.  
- **B)** Add a new command `roster_sweep` that either calls the same script with a “journal” mode or reimplements the loop in the CLI and calls `log_signal` / `log_prop_ev_entry`.

**Recommendation:** Prefer **A** with `--journal`: one code path (offline_scan + gate + log), less drift. Document “roster_sweep” as the CLI entry point that runs props_scan with `--journal` if you want a separate command name for daily_ops.

---

### 6. Intraday CLV: definition and when it’s available

Plan: “Compute intraday CLV from LineStore (opening vs current snapshot) … clvLine > 0 means line moved in our favor since we first saw it.”

**Clarify:**  
- “Current” in a batch job = latest snapshot in LineStore for that (date, player, stat, book). So intraday CLV = (get_closing_line - get_opening_line) in the same file.  
- For **prop_ev** / **auto_sweep** at a single moment there may be only one snapshot; “opening” might be the same as “current”, so intraday CLV would be null/zero. Plan should state: “Intraday CLV only applied when at least two snapshots exist for (player, stat, book, date); otherwise skip gate or treat as pass.”

---

### 7. fit_calibration: output path and loading

Plan: “Output new models/prob_calibration_bins01.json”.

**Improvement:**  
- Specify how the EV engine loads calibration: env var, config, or filename convention (e.g. prefer `prob_calibration_bins01.json` when present, else `prob_calibration.json`).  
- If we run two calibrations (full-range vs bins-0-1), add a one-line note on comparing Brier on the **same** held-out set so the decision “use bins01 in production” is evidence-based.

---

### 8. Game total: market name and source

Plan: “add get_game_total(event_id) using **h2h** market”.

**Reality:** “h2h” usually means moneyline (who wins). Totals are a separate market (e.g. “totals” or “over_under” in many feeds).

**Change:** Use the **totals** (or equivalent) market for the game O/U line, not h2h. Specify the exact key/name in your odds source (OddsStore vs LineStore vs API response). If the key is different for Odds API vs local_history, document both or add a small adapter.

---

### 9. Opponent B2B data source

Plan: “is_b2b field from get_todays_games() output — check the opponent team’s schedule”.

**Reality:** `get_todays_games()` may not currently expose “opponent is B2B”. Backtest derives B2B from `b2b_team_ids` (teams that played the day before).

**Improvement:** Specify explicitly: “Compute opponent_is_b2b from the same source as backtest (e.g. local index or get_todays_games plus a ‘played yesterday’ check).” If get_todays_games doesn’t have it, add a helper that, given (opponent_abbr, game_date), returns whether that team played on (game_date - 1), and use that in both backtest and live projection.

---

### 10. eligible_stats vs BETTING_POLICY

Plan doesn’t mention that SIGNAL_SPEC currently has `eligible_stats: {"pts", "reb", "ast"}` while CLAUDE.md says reb was removed from BETTING_POLICY (stat whitelist) due to ROI.

**Improvement:** Before or in Phase 1, decide: either remove `reb` from SIGNAL_SPEC.eligible_stats to align with BETTING_POLICY, or document why reb stays eligible for signals but is excluded from betting. Avoids silent drift between “can log” and “can bet.”

---

### 11. daily_ops and --dry-run

Plan: “Sequence: collect_lines → roster_sweep → best_today → print summary. Flag --dry-run for testing without journal writes.”

**Improvement:** Define --dry-run precisely: e.g. “run roster_sweep (and best_today) but do not call DecisionJournal.log_signal / log_prop_ev_entry; print what would have been logged.” That way validation doesn’t pollute the journal.

---

### 12. Phase 2 expected volume

Plan: “400–600” bets after P2 with “maintain +15%+ ROI, 65%+ hitRate”.

**Reality:** P1 tightens the gate (fewer, higher-quality bets). P2 adds many more lines. So volume goes up a lot while quality might drop if roster_sweep has noisier lines (e.g. fewer books, stale snapshots).

**Improvement:** Add a “Phase 2 risk” line: “If roster_sweep adds low-CLV or single-book lines, hit rate may drop; consider requiring Pinnacle + intraday CLV (or min books) for roster_sweep-origin signals.” Optionally track signal source (prop_ev/auto_sweep vs roster_sweep) in context_json so you can compare ROI/hit rate by source post-launch.

---

## Summary table

| Area | Plan | Correction / improvement |
|------|------|---------------------------|
| _qualifies context | ctx.get("referenceBook") | Use prop_result.get("referenceBook") (no ctx) |
| referenceBook storage | log_prop_ev_entry context | Store in DecisionJournal.log_signal(context=...) only |
| Pinnacle gate | noVigUnder only | Use recommended-side no-vig (noVigOver vs noVigUnder) |
| Per-stat Pinnacle | Not in phases | Add pinnacle_min_no_vig_by_stat to spec (Phase 1a/1d) |
| roster_sweep | New command | Prefer extending props_scan with --journal to avoid two code paths |
| Intraday CLV | “opening vs current” | Define: require ≥2 snapshots; else skip or pass |
| Calibration output | prob_calibration_bins01.json | Define how EV engine loads it and how Brier comparison is run |
| Game total | “h2h market” | Use totals market; document exact key for each odds source |
| Opponent B2B | get_todays_games | Specify: same source as backtest (e.g. “played yesterday” helper) |
| eligible_stats | Not mentioned | Align reb with BETTING_POLICY or document exception |
| daily_ops --dry-run | “without journal writes” | Define: skip log_signal / log_prop_ev_entry; print would-be logs |
| P2 volume risk | 400–600, maintain ROI | Note risk of noisier roster_sweep lines; consider source tracking |
| Backtest JSON parsing | calib['pts'].get('realLineCalibBins') | realLineCalibBins is top-level r['realLineCalibBins']; calibrationByStat[stat] is a list |

Use this as a living checklist when implementing each phase; it should prevent API/spec mismatches and keep the plan aligned with the current codebase.
