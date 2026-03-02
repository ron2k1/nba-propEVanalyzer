# Lessons Learned

## 2026-03-01 — Multi-agent code review false positive rate

**Mistake:** 7-agent review (RepoScout, RiskReviewer, RefactorPlanner, etc.) produced 12 findings.
On user verification against actual source, the CRITICAL finding and both HIGH findings were false positives.
False positive rate: ~7/12 findings wrong or not applicable.

**Root cause:** Agents read snippets and inferred behavior without reading the full function context.
Specific failures:
- Claimed `stat=stat` missing at `backtest:694` — it was present, correctly scoped from outer loop at line 646
- Claimed `_build_reference_probs` read the projection value — it reads `referenceBook.noVigOver/noVigUnder` (book odds only)
- Claimed `used_real_line` was a URL string — it's a bool expression (`x is not None and y is not None`)
- Claimed `bestRecommendation` miss could corrupt journal — already guarded by `if best_ev:` and `if _qs and _best_ev:`

**Rules going forward:**
1. Never trust agent severity ratings on code logic without reading the specific lines and surrounding context yourself
2. Before promoting any finding to CRITICAL or HIGH, read the actual function — not just the line number
3. Multi-agent review is useful for finding *candidate* issues but requires manual verification before acting
4. When agents flag a "missing param" or "wrong type", check `.get()` usage and guard clauses in the same function first
5. False positives on CRITICAL findings are worse than missing real bugs — they waste implementation time and erode trust

**What was actually real:**
- Delete dead `handle_ev_command` shim at `ev_commands.py:381-383` (cosmetic)
- Add `.get("snapshots", {})` guard at `line_commands.py:58` (low-impact safety)

## 2026-03-02 — Stale team data from LeagueDashPlayerStats

**Mistake:** Used `LeagueDashPlayerStats` (single API call) to build player→team mapping for `roster_sweep`. Jalen Green showed as HOU (traded to PHX mid-season), Clint Capela showed on a wrong team. Produced phantom signals for players not on today's teams.

**Root cause:** `LeagueDashPlayerStats` aggregates across the full season. `TEAM_ABBREVIATION` reflects the team where the player logged the most stats, NOT their current team after a trade.

**Fix:** Switched to `CommonTeamRoster` for only today's playing teams (8 calls for 4 games). This reflects current rosters including mid-season trades. Also added `team_not_in_event` check: if the player's team doesn't match either team in the Odds API event, skip them.

**Rules going forward:**
1. Never use `LeagueDashPlayerStats` for current team assignment — it's stale after trades
2. Use `CommonTeamRoster` when you need current roster membership — it reflects trades immediately
3. Always validate that a player's team is actually in the event being processed (Odds API sometimes lists props for wrong events)
4. When enriching data from external APIs (Odds API), always cross-validate against a second source (NBA API rosters)
5. Name normalization is required: strip `.`, `-`, `'` before matching Odds API names to NBA API names (e.g., "P.J. Washington" vs "PJ Washington")

## 2026-03-02 — Stale events in LineStore from late-night UTC rollover

**Mistake:** Full scan included yesterday's games (DET@ORL, PHI@BOS, OKC@DAL etc.) because `collect_lines` runs after midnight UTC filed snapshots under today's date while those games were still live.

**Root cause:** LineStore JSONL files are date-keyed by UTC timestamp. A game starting at 10 PM ET on Mar 1 has a commence_time of Mar 2 03:00 UTC. Snapshots collected at midnight+ UTC go into the Mar 2 file. The `roster_sweep` and scan read ALL snapshots for the date without filtering for current games.

**Fix:** Added game-day matchup filter to `roster_sweep`: fetch today's actual games via `get_todays_games()` (NBA scoreboard), build a set of `frozenset({home, away})` matchups, and only keep snapshots whose event appears in that set.

**Rules going forward:**
1. Always filter LineStore snapshots against today's NBA schedule before processing — never trust the JSONL date alone
2. The `get_todays_games()` scoreboard is the source of truth for which games are today
3. Any pipeline that reads LineStore snapshots must apply this filter (roster_sweep, scan, top_picks)
