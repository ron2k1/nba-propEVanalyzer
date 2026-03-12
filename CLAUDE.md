# CLAUDE.md

## 1. Mission + Project Map

Private NBA player-prop EV engine. **Stat priority:** pts > reb > ast > pra > tov >> stl > fg3m > blk. **Book priority:** BetMGM > DraftKings > FanDuel. **Forward estimate: +20-30% ROI** anchored on OOS bin-0 performance.

- blk/fg3m: structural Poisson bias — do not act on >60% confidence until recalibrated
- Real-line edge beats synthetic-line confidence. CLV (`clvLine > 0` AND `clvOddsPct > 0`) is the primary validity signal
- Synthetic ROI is a calibration diagnostic, not a real-money estimate

```
core/           21 engine modules (EV, projection, odds, journal, backtest — FROZEN)
nba_cli/        16 CLI command handlers (FROZEN)
scripts/        48 standalone utilities (calibration, backfill, analysis, MCP server)
web/            Alpine.js frontend (index.html + 8 modules, 7 tabs)
models/         prob_calibration.json, policy_history.json, walk_forward/
data/           runtime data (gitignored: SQLite, JSONL, journals, odds)
docs/           plans/, runbooks/, guides/, reports/, prompts/ — see docs/README.md
tasks/          lessons.md, FREEZE_2026-03-01.md
tests/          test_betting_policy, test_compute_ev, test_gates
```

## 2. Recent Changes

- **2026-03-12:** calibration refit from full-season data (Oct 21–Mar 02) — pts 1.81→3.29, ast 2.24→1.29; WF files regenerated; monotonicity enforcement added to fit_calibration.py
- **2026-03-07:** freeze lifted - experimentation allowed again on branches with verification + documentation
- **2026-03-05:** reb signal leak fixed — `gate_check()` now filters by BETTING_POLICY before GO-LIVE metrics
- **2026-03-04:** match-live `stat_whitelist` fix in `nba_backtest.py` (162 phantom reb bets removed)
- **2026-03-03:** blend disabled (`no_blend=True`), bins tightened to 0+9 only (bins 1+8 blocked)
- **2026-03-01:** `min_edge=0.08`, `min_confidence=0.60`, **FREEZE started** — no model/calibration/policy changes
- **2026-02-28:** reb removed from stat_whitelist (-5.34% ROI)

## 3. Current State (Freeze Lifted)

**BETTING_POLICY** (`core/nba_data_collection.py`):
- `stat_whitelist`: `{pts, ast}` — only these count for GO-LIVE gate
- `blocked_prob_bins`: `{1,2,3,4,5,6,7,8}` — **active bins: 0 (UNDER) + 9 (OVER) only**
- `no_blend=True` default in `compute_prop_ev()`

**SIGNAL_SPEC** (`core/nba_decision_journal.py` + `core/gates.py`):
- `eligible_stats`: `{pts, reb, ast}` — reb logged for research, NOT for betting
- `min_edge`: 0.08 | per-stat: `{reb: 0.08, ast: 0.09}`
- `min_confidence`: 0.60 | `real_line_required_stats`: `{reb}`

**Two-layer architecture:** `gate_check()` returns: (1) **metrics** — policy-qualified (pts+ast), drives GO-LIVE; (2) **model_leans** — all eligible signals; (3) **research_stats** — eligible but not in whitelist (reb); (4) **edge_at_emission** — pick-time edge stats.

**Calibration temps** (refitted 2026-03-12 from full-season data): `pts=3.29 reb=1.00 ast=1.29 fg3m=1.00 pra=3.27 stl=1.00 blk=1.00 tov=1.00`

**Change policy:** projection, calibration, and gating experiments are allowed again, but only on branches with explicit verification. Forward paper-trading summaries remain the source of truth for live deployment decisions.

## 4. Workflow Principles

1. **Plan first** — enter plan mode for non-trivial tasks (3+ steps). If something goes sideways, STOP and re-plan
2. **Subagent strategy** — offload research/exploration to subagents. One task per subagent
3. **Self-improvement loop** — after ANY correction: update `tasks/lessons.md`. Review at session start
4. **Verify before done** — never mark complete without proof. Run tests, check logs, demonstrate correctness
5. **Autonomous bug fixing** — just fix it. No hand-holding. Point at logs, then resolve
6. **Simplicity first** — minimal changes. Find root causes. Senior developer standards

**Roles:** Claude/Cursor writes features. Codex reviews diffs. Ollama (`gpt-oss:20b` @ `localhost:11434`) does runtime LLM; Claude (`claude-sonnet-4-6`) is fallback.

## 5. Environment

- Python 3.14, `.venv/` — all commands: `.\.venv\Scripts\python.exe`
- WinError 10013 (stats.nba.com blocked) → use `run_ui.ps1` with admin elevation
- `.env` (never committed): `ODDS_API_KEY`, `NEWS_API_KEY`, `ANTHROPIC_API_KEY`, `LLM_PROVIDER_ORDER=ollama_first`
- Setup: `python -m venv .venv && .\.venv\Scripts\python.exe -m pip install -r requirements.txt`

## 6. Key Commands

Last stdout line is always parseable JSON. Use `--model full` unless testing simple.

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
# Paper trading
.\.venv\Scripts\python.exe nba_mod.py collect_lines --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra
.\.venv\Scripts\python.exe nba_mod.py line_bridge --books betmgm,draftkings,fanduel --stats pts,reb,ast,pra
.\.venv\Scripts\python.exe nba_mod.py paper_settle 2026-03-01
.\.venv\Scripts\python.exe nba_mod.py paper_summary --window-days 14
.\.venv\Scripts\python.exe nba_mod.py journal_gate
# Backtest (no-lookahead: date_to must be before today)
.\.venv\Scripts\python.exe nba_mod.py backtest 2026-01-26 2026-02-25 --model full --local --save
.\.venv\Scripts\python.exe nba_mod.py minutes_eval 2026-01-26 2026-02-25 --local
# Lines & CLV
.\.venv\Scripts\python.exe nba_mod.py odds_build_closes
# Quality gate (required before every commit)
.\.venv\Scripts\python.exe scripts\quality_gate.py --json
```

## 7. Architecture & Import Policy

Engine logic in `core/`. CLI in `nba_cli/`. Entrypoints (`server.py`, `nba_mod.py`) contain no engine imports.

- `core/` → relative imports: `from .nba_X import Y`
- `nba_cli/` and `scripts/` → absolute: `from core.nba_X import Y`
- New commands → wire in `nba_cli/router.py` + handler. Never add logic to `nba_mod.py`
- Data paths in `core/` → use `dirname(dirname(__file__))` for repo root

| What | Where |
|------|-------|
| API/cache + BETTING_POLICY | `core/nba_data_collection.py` |
| Projections + shrinkage | `core/nba_prep_projection.py` |
| EV math (Normal/Poisson) | `core/nba_ev_engine.py` |
| Props/sweep/line matching | `core/nba_prop_engine.py` |
| Decision journal + SIGNAL_SPEC | `core/nba_decision_journal.py` |
| Gates + two-layer signals | `core/gates.py` |
| Odds store + closing lines | `core/nba_odds_store.py` |
| Bet tracking/settlement | `core/nba_bet_tracking.py` |
| Minutes model | `core/nba_minutes_model.py` |
| CLI commands | `nba_cli/router.py` + `*_commands.py` |

See `docs/architecture.md` for full module map and data layer details.

## 8. EV Engine Rules

`compute_ev()`: always pass `stat=` for temperature-scaling calibration. Poisson for `{stl,blk,fg3m,tov}`, Normal CDF for others. Edge computed vs **no-vig fair probability**.

Verdicts: `<0` Negative EV | `<0.08` Thin Edge | `0.08–0.12` Good Value | `>=0.12` Strong Value.

**GO-LIVE gate:** `paper_summary` → `gate.gatePass`: `sample >= 50` | `roi > 0.0` | `positive_clv_pct >= 50.0` | no stat with >=20 signals AND hit rate < 45%.

See `docs/ev-rules.md` for calibration temps, per-bin overrides, accuracy baselines, and pre-GO checklist.

## 9. Quality Gates & Definition of Done

```powershell
# Required before every commit
.\.venv\Scripts\python.exe scripts\quality_gate.py --json
# Smoke tests — run after any core/ change
.\.venv\Scripts\python.exe nba_mod.py games
.\.venv\Scripts\python.exe nba_mod.py prop_ev "Anthony Edwards" ORL 1 pts 25.5 -110 -110 0 MIN
```

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

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `WinError 10013` | stats.nba.com blocked | `run_ui.ps1` with admin elevation |
| `No module named 'nba_X'` | Missing `core.` prefix | `from core.nba_X import ...` |
| Cache resolves into `core/` | Extra `dirname()` missing | Use `dirname(dirname(abspath(__file__)))` |
| Calibration no-op in backtest | `stat=` missing | Add `stat=stat` to `compute_ev()` |
| `probOver` unchanged post-cal | `_PROB_CAL` empty | Verify `models/prob_calibration.json` exists |
| `realLineSamples=0` | Team name mismatch | Check `_ABBR_TO_NAME_PART` in `nba_odds_store.py` |
| `--local` falls back to API | `date_to` too recent | Use earlier `date_to` or rebuild index |
| Signal not logged | Filter blocked | Stat/edge/confidence/bin blocked by `_qualifies` |
| LLM returns stub | Ollama offline | Set `LLM_PROVIDER_ORDER=claude_first` in `.env` |

## 12. Deep Dives

- `docs/ev-rules.md` — calibration temps, per-bin overrides, Brier targets, accuracy baselines, Poisson trap
- `docs/runbooks/paper-trading.md` — daily routine, CLV rule, GO-LIVE gate details
- `docs/runbooks/backtest.md` — no-lookahead rules, local mode, calibration fit workflow
- `docs/runbooks/odds-backfill.md` — credit budgeting, chunk commands, resume
- `docs/architecture.md` — full module map, data layer, git exclusions
- `docs/plans/master-plan.md` — comprehensive GO-LIVE master plan
