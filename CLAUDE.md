# CLAUDE.md

## 1. Mission

Private NBA player-prop EV engine. **Stat priority:** pts > reb > ast > pra > tov >> stl > fg3m > blk. **Book priority:** BetMGM > DraftKings > FanDuel.

- blk/fg3m: structural Poisson bias — do not act on >60% confidence until recalibrated
- Real-line edge beats synthetic-line confidence. CLV (`clvLine > 0` AND `clvOddsPct > 0`) is the primary validity signal
- Synthetic ROI (+20–24%) is a calibration diagnostic, not a real-money estimate

### Accuracy Ceiling

**60d baseline (Dec 28–Feb 25, real lines, current policy):** 283 bets | 72.09% hit | +32.03% roiReal | 95% CI [22%, 42%]. Bins 0-10%: 79.5% hit/+46.4% ROI; 10-20%: 60.6%/+10.0%. Edge concentrated in extreme probability tails (UNDER confidence 0-20%, OVER confidence 80%+). CLV filtering is the next lever for live deployment.

**Do not:** chase BRef/RAPM/Bayesian priors for marginal accuracy gains. Do not try to beat 58% hit rate. Do not spend Odds API credits on fg3m/tov/stl/blk (not in stat whitelist). See `docs/PLAN_CLAUDE_CODE.md` for the full GO-LIVE master plan.

## 2. Workflow Principles

### Workflow Orchestration

1. **Plan Node Default** — enter plan mode for ANY non-trivial task (3+ steps or architectural decisions). If something goes sideways, STOP and re-plan immediately — don't keep pushing. Use plan mode for verification steps, not just building. Write detailed specs upfront to reduce ambiguity.
2. **Subagent Strategy** — use subagents liberally to keep main context window clean. Offload research, exploration, and parallel analysis to subagents. For complex problems, throw more compute at it via subagents. One task per subagent for focused execution.
3. **Self-Improvement Loop** — after ANY correction from the user: update `tasks/lessons.md` with the pattern. Write rules that prevent the same mistake. Ruthlessly iterate on these lessons until mistake rate drops. Review lessons at session start for relevant project.
4. **Verification Before Done** — never mark a task complete without proving it works. Diff behavior between main and your changes when relevant. Ask yourself: "Would a staff engineer approve this?" Run tests, check logs, demonstrate correctness.
5. **Demand Elegance (Balanced)** — for non-trivial changes: pause and ask "Is there a more elegant way?" If a fix feels hacky: "Knowing everything I know now, implement the elegant solution." Skip this for simple, obvious fixes — don't over-engineer. Challenge your own work before presenting it.
6. **Autonomous Bug Fixing** — when given a bug report: just fix it. Don't ask for hand-holding. Point at logs, errors, failing tests — then resolve them. Zero context switching required from the user. Go fix failing CI tests without being told how.

### Task Management

1. **Plan first** — write plan to `tasks/todo.md` with checkable items before starting
2. **Verify plan** — check in before starting implementation on non-trivial changes
3. **Track progress** — mark items complete as you go, explain changes at each step
4. **Explain changes** — high-level summary at each step
5. **Document results** — add review section to `tasks/todo.md` when done
6. **Capture lessons** — update `tasks/lessons.md` after corrections. Ruthlessly iterate until mistake rate drops

### Core Principles

- **Simplicity first** — make every change as simple as possible. Impact minimal code
- **No laziness** — find root causes. No temporary fixes. Senior developer standards
- **Minimal impact** — changes should only touch what's necessary. Avoid introducing bugs

### Roles

- **Claude/Cursor** — writes features, refactors, produces reports, plans
- **Codex** — reviews diffs, debugs import/logic errors
- **Ollama** (`gpt-oss:20b` @ `localhost:11434`) — runtime LLM inference; Claude (`claude-sonnet-4-6`) is fallback

## 3. Environment

- Python 3.14, `.venv/` — all commands: `.\.venv\Scripts\python.exe`
- WinError 10013 (stats.nba.com blocked) → use `run_ui.ps1` with admin elevation
- `.env` (never committed): `ODDS_API_KEY`, `NEWS_API_KEY`, `ANTHROPIC_API_KEY`, `LLM_PROVIDER_ORDER=ollama_first`
- Setup: `python -m venv .venv && .\.venv\Scripts\python.exe -m pip install -r requirements.txt`

## 4. Key Commands

Last stdout line is always the parseable JSON payload. Use `--model full` unless testing simple.

```powershell
# Server
.\.venv\Scripts\python.exe server.py                    # http://127.0.0.1:8787
powershell -ExecutionPolicy Bypass -File ".\run_ui.ps1" -OddsApiKey "KEY"
# Projection & EV
.\.venv\Scripts\python.exe nba_mod.py projection "Anthony Edwards" ORL 1 0
.\.venv\Scripts\python.exe nba_mod.py prop_ev "Anthony Edwards" ORL 1 pts 25.5 -110 -110 0 MIN
.\.venv\Scripts\python.exe nba_mod.py auto_sweep "Anthony Edwards" MIN ORL 1 pts 0 us "betmgm,draftkings,fanduel" basketball_nba 15
# Daily ops
.\.venv\Scripts\python.exe nba_mod.py best_today 15
.\.venv\Scripts\python.exe nba_mod.py top_picks 5
.\.venv\Scripts\python.exe nba_mod.py settle_yesterday
.\.venv\Scripts\python.exe nba_mod.py results_yesterday 50
# Data queries
.\.venv\Scripts\python.exe nba_mod.py games
.\.venv\Scripts\python.exe nba_mod.py player_lookup "anthony edwards" 10
.\.venv\Scripts\python.exe nba_mod.py player_log "Anthony Edwards" 20
.\.venv\Scripts\python.exe nba_mod.py roster_status MIN
.\.venv\Scripts\python.exe nba_mod.py defense
# Decision journal & paper trading
.\.venv\Scripts\python.exe nba_mod.py paper_settle 2026-03-01
.\.venv\Scripts\python.exe nba_mod.py paper_summary --window-days 14
.\.venv\Scripts\python.exe nba_mod.py journal_gate
# Backtest (no-lookahead: date_to must be before today)
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-01-26 2026-02-25 --model full --local --save
.\.venv\Scripts\python.exe nba_mod.py minutes_eval 2026-01-26 2026-02-25 --local
# Lines & CLV
.\.venv\Scripts\python.exe nba_mod.py collect_lines --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra
.\.venv\Scripts\python.exe nba_mod.py line_bridge --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes
# Quality gate (required before every commit)
.\.venv\Scripts\python.exe scripts\quality_gate.py --json
```

## 5. Architecture & Import Policy

Engine logic in `core/`. CLI in `nba_cli/`. Entrypoints (`server.py`, `nba_mod.py`) contain no engine imports.

- `core/` → relative imports: `from .nba_X import Y`
- `nba_cli/` and `scripts/` → absolute: `from core.nba_X import Y`
- New commands → wire in `nba_cli/router.py` + handler. Never add logic to `nba_mod.py`

| What | Where |
|------|-------|
| API/cache | `core/nba_data_collection.py` |
| Projections | `core/nba_prep_projection.py` |
| EV math | `core/nba_ev_engine.py` |
| Props/sweep | `core/nba_prop_engine.py` |
| Decision journal | `core/nba_decision_journal.py` |
| Minutes model | `core/nba_minutes_model.py` |
| CLI commands | `nba_cli/router.py` + `*_commands.py` |

## 6. EV Engine Rules

`compute_ev()`: always pass `stat=` for temperature-scaling calibration. Poisson for `{stl,blk,fg3m,tov}`, Normal CDF for others. Edge computed vs **no-vig fair probability**.

**BETTING_POLICY** (in `nba_data_collection.py`): stat whitelist `{pts, ast}` (reb removed 2026-02-28: -5.34% ROI; pra removed 2026-03-01: -3.81% ROI), blocked bins `{2,3,4,5,6,7}` (20–80% calibrated range; bin 7 added 2026-03-01: 51.4% hit / -9.88% ROI on 107 real-line bets, 60d). Active betting bins: 0-10% and 10-20% (UNDER confidence) + 80-100% (OVER confidence).

**SIGNAL_SPEC** (in `nba_decision_journal.py`): `min_edge = 0.08`, `min_edge_by_stat = {reb: 0.08, ast: 0.09}` (ast raised 2026-03-01: -1.11% ROI on 2,255 bets), `min_confidence = 0.60` (raised 2026-03-01: was 0.55), `real_line_required_stats = {reb}`. Signals auto-logged on qualifying `prop_ev`/`auto_sweep` calls; deduped by `(player_id, stat, book, line, date)`.

Verdicts: `<0` Negative EV | `<0.08` Thin Edge | `0.08–0.12` Good Value | `≥0.12` Strong Value.

## 7. Calibration & Backtest

**No-lookahead rule:** `date_to` must be strictly before today. Never feed future outcomes to the model.

```powershell
# Full backtest — zero API calls
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-01-26 2026-02-25 --model full --local --save
# With real closing lines
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-01-26 2026-02-25 --model full --local --odds-source local_history --save
# Minutes model evaluation
.\.venv\Scripts\python.exe nba_mod.py minutes_eval 2026-01-26 2026-02-25 --local
# Fit calibration from backtest
.\.venv\Scripts\python.exe scripts\fit_calibration.py --input data/backtest_results/2026-01-26_to_2026-02-25_full_local.json --output models/prob_calibration.json
# 60-day backtest + auto-log to data/backtest_60d_log.jsonl (run weekly)
.\.venv\Scripts\python.exe nba_mod.py backtest_60d           # date_to = yesterday
.\.venv\Scripts\python.exe nba_mod.py backtest_60d 2026-02-27  # explicit date_to
# Odds API historical backfill (use --resume + --max-requests to cap credits)
.\.venv\Scripts\python.exe nba_mod.py odds_backfill 2025-10-01 2026-02-27 --books betmgm,draftkings,fanduel --stats pts,ast,pra --offset-minutes 60 --max-requests 90000 --resume
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes 2025-10-01 2026-02-27
```

Current temps (refitted 2026-03-01, 87d Dec 1–Feb 25, `--min-pred 0.01 --max-pred 0.25`): `pts=1.81 reb=3.79 ast=2.24 fg3m=1.49 pra=1.77 stl=1.39 blk=1.30 tov=1.25 _global=1.77`
Per-bin temps: `pts: {0-10: 1.32, 10-20: 2.71}` | `ast: {0-10: 1.76, 10-20: 3.21}` | `reb: {0-10: 1.00, 10-20: 3.79}` | `pra: {0-10: 1.38, 10-20: 2.45}` | `blk: {0-10: 1.44, 10-20: 1.14}`

Real-line ROI (60d Dec 28–Feb 25, post-bin7-block 2026-03-01): `pts=+29.9% ast=+35.0%` | overall roiReal=+32.03% on 283 bets | 72.09% hit rate. Active bins: 0-20% (UNDER) = 270 bets / 71.5% hit; 80-100% (OVER) = 13 bets / 69.2% hit.

### Pre-GO Calibration Checklist

- [ ] Avg Brier < 0.235 (current baseline: 0.2325)
- [ ] All 8 stats show Brier improvement vs uncalibrated
- [x] Minutes 35+ bucket bias < ±3.0 min (fixed 2026-02-28: +6.24→+2.39 min, _DECAY=0.30)
- [ ] `models/prob_calibration.json` exists; `_fitted_at` < 60 days old
- [ ] blk/fg3m 60–80% bin gaps documented; NOT used as GO signals
- [ ] reb 60-70% bin gap < 12% (currently 9.2% post-cal)

## 8. Live Edge Pipeline

CLV rule: `clvLine > 0` AND `clvOddsPct > 0` required for high-quality bets. Positive model EV alone is not sufficient.

### Daily Paper-Trading Routine (Game Days)

```powershell
# Before games (2-3x: 11am, 2pm, 5pm ET)
.\.venv\Scripts\python.exe nba_mod.py collect_lines --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra
.\.venv\Scripts\python.exe nba_mod.py best_today 20
# Pre-tip (6:30 PM ET) — top picks + best parlay
.\.venv\Scripts\python.exe nba_mod.py top_picks 5
# End-of-day
.\.venv\Scripts\python.exe nba_mod.py line_bridge --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes
# Next morning
.\.venv\Scripts\python.exe nba_mod.py paper_settle <yesterday>
.\.venv\Scripts\python.exe nba_mod.py results_yesterday 50
.\.venv\Scripts\python.exe nba_mod.py paper_summary --window-days 14
```

### Odds Backfill

`--max-requests 1950` per chunk ≈ 19,500 credits. Start with `pts,reb,ast`. Bridge is idempotent (`INSERT OR IGNORE`). See `docs/PLAN_CLAUDE_CODE.md` for full runbook and chunk commands.

### GO-LIVE Gate

`paper_summary` → `gate.gatePass`: `sample >= 50` | `roi > 0.0` | `positive_clv_pct >= 50.0` | no stat with ≥20 signals AND hit rate < 45%.

**Model comparison protocol:** same date range, same books/stats, same odds DB. Compare `roiReal`, `hitRate` on real-line subset, and `realLineSamples` count only.

## 9. Quality Gates & Definition of Done

```powershell
# Required before every commit
.\.venv\Scripts\python.exe scripts\quality_gate.py --json
# Required before any GO decision
.\.venv\Scripts\python.exe scripts\quality_gate.py --full --json
# Smoke tests — run after any core/ change
.\.venv\Scripts\python.exe nba_mod.py games
.\.venv\Scripts\python.exe nba_mod.py prop_ev "Anthony Edwards" ORL 1 pts 25.5 -110 -110 0 MIN
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-02-19 2026-02-22 --model full --local
```

Pass: `quality_gate.py` → `"ok": true`; `prop_ev` → `"success": true` with `distributionMode`; backtest → `sampleCount > 0`, errors < 5%.

Hard blockers (do not commit):
- `python_compile` failure in `core/` or `nba_cli/`
- `compute_ev()` without `stat=` in any loop
- Data paths inside `core/` not using `dirname(dirname(__file__))` for repo root
- Keys, raw data, or `.pkl` files in committed changes

A change is done when ALL pass:
- [ ] `quality_gate.py --json` → `"ok": true`
- [ ] `prop_ev` smoke → `"success": true` with `distributionMode` field
- [ ] Every new `compute_ev()` call includes `stat=<stat_key>`
- [ ] Projection change → short backtest smoke passes (`sampleCount > 0`)
- [ ] Calibration change → Brier scores documented pre/post
- [ ] Import policy: `core/` relative; `nba_cli/`+`scripts/` absolute

## 10. Safety & Data Paths

- `.env` never committed. All keys via `os.getenv()` only
- **Commit:** `.py`, `.js`, `.html`, `requirements.txt`, `models/prob_calibration.json`
- **Never commit:** `.env`, `.nba_cache/`, `data/`, `models/*.pkl`

Data: `data/line_history/YYYY-MM-DD.jsonl` | `data/alerts/YYYY-MM-DD.jsonl` | `data/decision_journal/decision_journal.sqlite` | `data/reference/odds_history/odds_history.sqlite` (all gitignored via `data/`)

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `WinError 10013` | stats.nba.com blocked | `run_ui.ps1` with admin elevation |
| `No module named 'nba_X'` | Missing `core.` prefix | `from core.nba_X import ...` |
| Cache resolves into `core/` | Extra `dirname()` missing | Use `dirname(dirname(abspath(__file__)))` |
| Calibration no-op in backtest | `stat=` missing | Add `stat=stat` to `compute_ev()` |
| `probOver` unchanged post-cal | `_PROB_CAL` empty | Verify `models/prob_calibration.json` exists |
| `realLineSamples=0` | Team name mismatch | Check `_ABBR_TO_NAME_PART` in `nba_odds_store.py` |
| `missingLineSamples` high | Snapshots not built | Run `backfill_odds_history` + `build_closing_lines` |
| `--local` falls back to API | `date_to` too recent | Use earlier `date_to` or rebuild index |
| Signal not logged | Filter blocked | Stat/edge/confidence/bin blocked by `_qualifies` |
| `journalDuplicateSignal` | Same-day re-run | Expected — one signal per `(player, stat, book, line, date)` |
| LLM returns stub | Ollama offline | Set `LLM_PROVIDER_ORDER=claude_first` in `.env` |
