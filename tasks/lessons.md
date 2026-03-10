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

## 2026-03-02 — Validation script missing --odds-source local_history

**Mistake:** `validate_shrink_k.py` ran backtests with `--model full --local --emit-bets` but omitted `--odds-source local_history`. The sensitivity sweep that selected k=8 used real closing lines. Without them, the validation used synthetic lines → completely different bet population (92 bets vs expected ~375), implausibly high ROI (66% vs ~36%), making all downstream statistics meaningless.

**Root cause:** Agent building the script didn't know the original sensitivity sweep used real lines. The plan spec said `--model full --local --emit-bets` without mentioning odds source.

**Fix:** Added `--odds-source local_history` to the subprocess command in `validate_shrink_k.py`.

**Rules going forward:**
1. Any backtest comparison must use the same flags as the original run — especially `--odds-source`. Always verify bet counts match expectations before interpreting results
2. When bet counts differ by >20% from expectations, stop and diagnose before interpreting
3. Hit rates >80% on real-money-style backtests are a red flag — verify the bet population

## 2026-03-02 — Validation decision criteria: underpowered tests should preserve priors

**Mistake:** Decision criteria said "revert if p > 0.30" — this makes sense when you expect a detectable effect, but k=8 vs k=12 has a ~1.8pp effect size requiring ~2,000+ bets to detect. The test was underpowered by design, so "inconclusive" triggered "revert" when the point estimate actually favored keeping k=8.

**Fix:** Changed revert logic to "revert if k_b outperforms on point estimate AND p < 0.30." Inconclusive results now preserve the prior rather than overturning it.

**Rules going forward:**
1. Set decision criteria AFTER understanding the effect size, or build in a "preserve prior" default for underpowered tests
2. "Can't prove A is better" ≠ "B is better" — asymmetric burden of proof should match deployment reality
3. When two options are equivalent within measurement precision, keep the status quo and move to higher-leverage work

## 2026-03-02 — Small projection changes affect bet selection, not bet accuracy

**Finding:** k=8 vs k=12 validation showed 338 shared bets with identical ROI to six decimal places. Projection shifts of 0.0–0.3 points never flipped a single outcome. The entire ROI difference between the two k values came from ~37 marginal bets that crossed edge/bin thresholds under one k but not the other.

**Implication:** Any projection tweak under ~0.3 points is invisible to accuracy metrics (hit rate, Brier, aggregate ROI) but changes the bet population at the margins. These marginal bets — the ones that barely qualify or barely don't — are where projection changes have real P&L impact.

**Rules going forward:**
1. For small projection changes (<0.3 pts), evaluate by diffing the bet population (which bets are added/removed), not by comparing aggregate hit rates
2. Use `--emit-bets` to get bet-level records, then diff on `(date, player_id, stat, side)` between old and new
3. The marginal bets that flip in/out are the signal — compute their ROI separately
4. Aggregate hit rate comparisons are only meaningful when projection shifts are large enough to change outcomes on shared bets (likely >1 point)

## 2026-03-05 — SIGNAL_SPEC vs BETTING_POLICY stat leak in GO-LIVE gate

**Mistake:** `gate_check()` counted ALL journal signals (including reb) toward the GO-LIVE gate sample, even though BETTING_POLICY only whitelists {pts, ast}. Paper trading showed 26 settled / 21W / 5L (80.8%), but the real policy-qualified number was 20 settled / 16W / 4L (80%).

**Root cause:** `_qualifies()` in `gates.py` checks `SIGNAL_SPEC.eligible_stats = {pts, reb, ast}` (for research/CLV tracking). `gate_check()` in `nba_decision_journal.py` queried all settled signals without filtering by `BETTING_POLICY.stat_whitelist`. This is the same two-layer gap that existed in the backtest (fixed 2026-03-04) but had not been fixed in the live journal path.

**Fix:** Added BETTING_POLICY import and stat_whitelist filter to `gate_check()`. reb signals remain in the journal (intentional research tracking) but are excluded from gate metrics. Added `research_stats` and `model_leans` sections to gate output to show both ledgers explicitly.

**Rules going forward:**
1. Any function that computes GO-LIVE metrics must filter by BETTING_POLICY.stat_whitelist, not just SIGNAL_SPEC.eligible_stats
2. Always verify sample counts match expectations before interpreting gate results — 6 extra reb signals inflated the sample by 30%
3. Two-layer architecture (SIGNAL_SPEC for research, BETTING_POLICY for betting) requires explicit filtering at every aggregation point
4. The backtest, paper trading, and live pipeline must all enforce the same policy — check all three when adding a new filter

## 2026-03-05 — Backtest vs paper trading conflation & Poisson accuracy trap

**Mistake:** Model leans analysis presented 60d backtest (297 bets, 84.2% hit, +56% ROI) alongside paper trading numbers without clearly separating them. Also flagged fg3m (96.4%) and blk (88.5%) accuracy as "tempting" opportunities.

**Corrections (from user):**

1. **Backtest is not live performance.** The 297 bets at 84.2%/+56% ROI on Dec 28–Feb 25 is the *in-sample* result — calibration temps were fitted on this same period. The actual paper trading record is ~20 settled pts+ast bets at 80% hit rate. That's the only number that matters for the go-live decision.

2. **fg3m/blk accuracy is a Poisson distribution artifact, not model skill.** A player projected for 1.8 threes with a line of 2.5 will almost always go under — not because the model is brilliant, but because Poisson distributions are heavily right-skewed at low means. The "accuracy" is mostly the distribution shape doing the work. Until Poisson calibration temps are verified and ROI is positive after juice, these are noise.

3. **Accuracy and ROI diverge when lines are sharp.** pra at 83.1% accuracy with -3.81% ROI means the model picks the right direction but the books price it correctly — no edge after juice. The -3.81% was measured under the old blended config. Post-freeze: re-evaluate pra under no-blend + bins 0+9 to see if ROI turns positive.

4. **The edge is narrow and specific.** Bin 0 unders on pts and ast using Normal CDF. That's the entire signal. Everything else is either blocked for good reason or unvalidated. Don't get distracted by blocked stats showing high accuracy.

**Rules going forward:**
1. Never present backtest numbers and paper trading numbers in the same table without explicit "IN-SAMPLE" / "PAPER TRADING" / "OOS" labels
2. For Poisson stats (fg3m, blk, stl, tov): accuracy is meaningless without ROI-after-juice. The distribution does the prediction, not the model
3. When accuracy is high but ROI is negative → the books are efficient on that market. Direction != edge
4. Stay focused on accumulating paper trades for the proven signal (bin 0 unders, pts+ast, Normal CDF). Volume on that signal is the bottleneck, not expanding to more stats
5. Any post-freeze pra re-evaluation must compare ROI under current config (no-blend + bins 0+9), not accuracy

## 2026-03-07 — Classifier variant swapping is low-leverage; feature leakage traps

**Findings from Phase 2-C training cycle:**

1. **More classifiers != better performance.** GBC pts+ast (0.6302 AUC) vs XGBoost (0.5892) vs LightGBM (0.6066). Swapping classifiers on the same features gives diminishing returns. The all-stats GBC on 56K rows (0.6478 AUC) beats all pts+ast-filtered variants because more data > better algorithm.

2. **Isotonic calibration has a sample-size threshold.** On 12.7K rows (pts+ast): Brier worsened 0.2344 -> 0.2362. On 56K rows (all stats): Brier improved 0.2200 -> 0.2195. The 5-fold isotonic estimator needs enough data per fold. Don't calibrate with <20K training rows.

3. **Feature leakage gives false positives that look amazing.** Per-stat ML regression with auto-inferred features (including closingLine, pnl, clvDelta) showed pts MAE dropping from 4.66 to 3.14 (-32.6%). With clean features (no post-settlement data): MAE rose to 5.10 — *worse* than raw projection. The Bayesian shrinkage engine already uses all available pick-time information.

4. **Quantile regression shows real but modest gain.** Clean-feature quantile P(over) vs Normal CDF: Brier 0.2216 vs 0.2249 (+1.49%). Real because it captures stat distribution skewness, but not large enough to justify replacing Normal CDF in production yet.

**Rules going forward:**
1. Always explicitly specify feature lists for ML training — never rely on auto-inference from JSONL columns. Block `closingLine`, `closingOverOdds`, `closingUnderOdds`, `clvDelta`, `clvOddsPct`, `pnl`, `outcome`, `actual`, `player_id`
2. When ML holdout MAE < raw projection MAE by >20%, suspect leakage — verify no post-settlement features in the feature set
3. Prefer more training rows over stat-specific filtering for outcome classifiers
4. Isotonic calibration: use only when training set > 20K rows

## 2026-03-09 — Model Audit Implementation (30+ fixes)

**Changes implemented across 10 files, all verified with 330 tests passing:**

1. **CLV gate neutral pass (gates.py):** Changed `<= 0` to `< 0` — neutral CLV (zero) no longer blocks signals. Mixed-sign still blocks correctly.

2. **Minutes volatility reordering (nba_minutes_model.py):** Moved volatility dampening AFTER streak/trend signals 2-6. Previously it ran first when multiplier=1.0 (no-op). Now it actually compresses extreme multipliers. Added directional guard (`abs > 0.01`).

3. **Minutes multiplier floor naming (nba_minutes_model.py):** Dual-floor system: `_MULTIPLIER_NORMAL_MIN=0.85` for model signals, `_MULTIPLIER_ABSOLUTE_MIN=0.50` for injury caps. Docstring updated.

4. **Stdev shrinkage by sample size (nba_prep_projection.py):** Replaced fixed 0.75 factor with `max_shrink * min(1.0, n/25)`. At n=5: 0.15x shrinkage. At n=25: full 0.75x. Env overrides set the max factor.

5. **Combo stat stdev (nba_prep_projection.py):** Same n-dependent shrinkage applied to PRA/combo stats. Added diagnostic `componentStdevSum` field for PRA calibration comparison.

6. **Mass absence model (nba_prep_usage.py):** Three tiers (normal/moderate/extreme) based on absent starters (`max(seasonMin, recentMin) >= 28`). Lower USG threshold in extreme tier (12% vs 18%). Higher caps (2.00 vs 1.45). Minutes model gets roster_context for starter boost.

7. **Usage adjustment scope (nba_prep_projection.py):** Usage/mass-absence is LIVE-ONLY. `get_team_roster_status()` uses `last_n_games=5` from *now*, not from `as_of_date`, so running it in backtests would be lookahead. The `if player_team_abbr:` guard keeps it live-only (backtest doesn't pass that arg). See Finding 1 in §2026-03-09 below.

8. **Policy config module (core/policy_config.py):** Single source of truth for STAT_WHITELIST, BLOCKED_PROB_BINS, ELIGIBLE_STATS, MIN_EDGE, etc. Both gates.py and nba_data_collection.py import from it.

9. **Role-change threshold (nba_prep_projection.py):** Made relative to season avg: `max(3.0, seasonMin * 0.15)` instead of fixed 5.0. Bench players (15min) trigger at 3min delta, starters (32min) at 4.8min.

10. **ML feature importances (nba_model_ml_training.py):** `_extract_feature_importances()` extracts from tree-based or linear models. Added to both outcome classifier and projection training output dicts.

11. **Defense rank weighting (nba_prep_projection.py):** Rank modulates multiplier distance from neutral. Top/bottom 5 ranks: 120% effect. Middle ranks: 80% effect. No double-counting.

**Rules going forward:**
1. Volatility dampening must run AFTER signals that move the multiplier, not before
2. Policy constants should live in `core/policy_config.py` — both gates.py and nba_data_collection.py import from there
3. Stdev shrinkage must be sample-dependent — fixed factors overfit to large-sample regime
4. Mass absence tiers use `max(seasonMin, recentMin) >= 28` for starter classification, not raw USG%

## 2026-03-09 — Post-audit code review findings (6 issues)

**Findings from code review of model audit implementation:**

1. **Usage adjustment is NOT date-aware (HIGH).** `get_team_roster_status()` uses `LeagueDashPlayerStats(last_n_games=5)` which returns last 5 games from *now*, not from `as_of_date`. Wiring `player_team_abbr` into the backtest caller would be lookahead. Fixed: corrected misleading "date-aware" comment, kept usage as live-only via the `if player_team_abbr:` guard (backtest doesn't pass it).

2. **Mass-absence boost missed promoted role players (MEDIUM).** Original threshold `avg_s >= 28.0` only helped established starters. Fixed: extreme tier now also boosts players with `avg_s >= 20.0` (1.04x vs 1.06x for starters) — these are the bench/fringe players who absorb vacated minutes.

3. **Defense rank mapping included unpopulated stats (MEDIUM).** `_STAT_TO_DEF_RANK` mapped stl/blk/tov but `get_team_defensive_ratings()` only populates OPP_*_RANK for pts/reb/ast/fg3m. Silently fell back to neutral rank=15. Fixed: removed stl/blk/tov from the mapping.

4. **Missing tests for new behavior (MEDIUM).** Added 6 tests to `test_minutes_model.py` for roster_context/mass-absence boost: extreme starter, extreme promoted, extreme bench (no boost), moderate starter, normal (no boost), missing context (no boost).

5. **Stale comments (LOW).** Fixed multiplier range in minutes model docstring (now "0.50–1.15"), fixed CLV rule description in test_betting_policy.py (now "< 0 OR < 0").

**Rules going forward:**
1. Never claim an API is "date-aware" without verifying the actual query parameters — `last_n_games=5` means "last 5 from today"
2. When a feature is live-only due to data limitations, the guard must be explicit and commented — don't rely on coincidental parameter absence
3. Any mapping dict must only reference fields that the data source actually populates — silent fallbacks to neutral hide bugs
4. New behavioral paths need tests BEFORE marking implementation complete — if the review has to find it, it was shipped untested

## 2026-03-09 — Second review pass (5 findings)

1. **(MEDIUM) No backtest path for usage/mass-absence.** Accepted as design limitation — usage is live-only because `last_n_games=5` returns from now. Corrected lesson #7 above (was contradictory).

2. **(MEDIUM) `_classify_absence_tier` missed deadline arrivals.** `seasonMin` alone misses mid-season trades/role changes. Fixed: now uses `max(seasonMin, recentMin) >= 28`. Both fields come from `get_team_roster_status()`. Added `test_recent_min_fallback_counts_as_starter` in `test_prep_usage.py`.

3. **(MEDIUM) Mass-absence tests too loose.** Tests checked `multiplier > 1.0` but code claims 1.06x/1.04x/1.03x. Fixed: tests now compute baseline without roster_context and verify `result ≈ baseline * boost_factor` with `pytest.approx(abs=0.005)`.

4. **(LOW) No tests for defense-rank modulation, role-change, feature-importance.** Fixed: added `tests/test_projection_signals.py` with 15 tests (6 defense-rank, 5 role-change, 4 ML feature-importance).

5. **(LOW) lessons.md contradiction.** Line 160 said "usage now runs in backtests" while line 180 said opposite. Fixed line 160.

**New rules:**
5. Starter classification should use `max(seasonMin, recentMin)` — catches deadline arrivals and recent role changes
6. Tests for specific multiplier factors must verify the exact factor relative to baseline, not just > 1.0 — prevents silent weakening in refactors

## 2026-03-09 — Third review pass (merge readiness)

1. **(HIGH) "Policy unchanged = safe to merge" is wrong.** Gates consume `probOver`, `edge`, `confidence`, and minutes-restriction tags from the projection pipeline (`nba_prop_engine.py:135,304`, `gates.py:68,93`). Stdev shrinkage changes `probOver`, mass-absence changes `confidence` via usage adjustment, defense weighting changes projection mean, minutes reorder changes restriction tags. All can flip a prop from pass → block or vice versa. Correct framing: "projection accuracy improvements → different props pass gates → need forward validation before live deployment."
2. **(MEDIUM) Branch 4 commits behind master.** Always sync with base branch and retest before merging.
3. **(MEDIUM) Scope understated (129 files, 22k insertions).** Large merges need honest scope description, not minimization.

**New rule:**
7. Never claim "policy unchanged = safe" for projection/calibration changes. The projection pipeline feeds gates — any change to mean, stdev, or multiplier can change which props pass or fail gating.

## 2026-03-09 — Fourth review pass (settlement pipeline hardening)

1. **(HIGH) `_find_game_row` soft-filter fallback.** When `opponent_abbr` or `is_home` were provided but didn't match, the function fell back to the first same-date row instead of returning None. Could grade a bet against the wrong game (e.g. post-trade or doubleheader). Fixed: opponent and is_home are now hard filters — no match → None. Same fix applied to decision journal settlement paths (lines 404, 748) which previously didn't pass `opponent_abbr` at all.
2. **(MEDIUM) `_extract_stat_from_row` trusted raw values.** `_as_float(row.get("PTS"), 0.0)` coerced missing/malformed values to 0.0 instead of failing. Fixed: now returns None for missing fields, negative values, or values exceeding per-stat ceilings (PTS>100, STL>15, etc.). Combo stats (PRA, PR, etc.) require all components valid.
3. **(MEDIUM) No final-status safety gate.** Settlement didn't verify the game-log row came from a completed game. PlayerGameLog normally only returns completed games, so the risk is low but the guard was absent. Fixed: added MIN-field presence check before grading.
4. **(LOW-corrected) Fuzzy name matcher false positive.** Last-name fallback in `nba_odds_store.py:177` only resolves when there is exactly one match; ambiguous cases return None. Already tested. Not a red-tier issue.
5. **(LOW-corrected) Cache corruption impact overstated.** `cache_get` returns None on error → triggers refetch, not stale data. `cache_set` can fail silently but doesn't serve bad data.
6. **(LOW-corrected) Quota enforcement overstated.** On non-200 responses, `_odds_api_get` returns `success: False`. System doesn't silently serve stale odds — it fails the operation.

**New rules:**
8. Settlement row matching must be strict — opponent_abbr and is_home are hard filters, never soft fallbacks.
9. Never coerce missing stat values to 0.0 — a missing field means the data isn't ready, not that the stat is zero.
10. Audit findings must distinguish confirmed failure modes from missing safety checks.
