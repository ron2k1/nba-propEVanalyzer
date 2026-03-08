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
