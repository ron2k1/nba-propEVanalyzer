# Post-Freeze Enhancements Plan (~2026-03-15+)

## Context
The engine runs 30+ calculations but has 4 gaps that could materially improve edge detection. The proven signal (bin 0 UNDER on pts+ast, +32% OOS ROI) comes from temperature calibration + Pinnacle anchoring + aggressive filtering. These enhancements strengthen the inputs flowing into that pipeline. A 5th feature adds Chart.js visualization to the UI.

All features are additive and opt-in — no changes to existing frozen calculation paths when new params are absent.

---

## Priority Order

| # | Feature | Rationale | Effort |
|---|---------|-----------|--------|
| 1 | Usage Rate Integration | Module EXISTS but disconnected. Wire into projection. Highest leverage. | Low |
| 2 | Opponent-Adaptive Stdev | Fix flat 0.75x shrinkage. Pace data already fetched. Direct calibration improvement. | Low |
| 3 | Market-Implied Projection Delta | One-line inverse CDF. Pinnacle data already available. High context value. | Trivial |
| 4 | Intraday CLV as Real-Time Signal | +CLV = +17.6% ROI vs -CLV = -22.7%. Start informational, gate later. | Medium |
| 5 | UI Math Visualization (Chart.js) | Distribution curves, line movement, edge scatter. Depends on 1-4 being surfaced. | Medium |

---

## Feature 1: Usage Rate Integration

**Problem:** `nba_prep_usage.py` computes per-stat usage multipliers when teammates are absent, but it's only applied post-hoc in CLI — never flows into the canonical projection.

**Integration point:** `core/nba_prep_projection.py` -> `compute_projection()`

**Changes:**
- `core/nba_prep_projection.py`: Add `player_team_abbr=None` param. Before stat loop, call `compute_usage_adjustment()`. In stat loop (after line 588), multiply `model_projection *= _usage_mults[stat]`. Guard: only in live mode (`as_of_date is None`) to prevent backtest lookahead.
- `core/nba_prop_engine.py`: Pass existing `player_team_abbr` through to `compute_projection()` in `compute_prop_ev()` (line 129) and `compute_auto_line_sweep()` (line 333).
- `nba_cli/ev_commands.py`: Deprecate post-hoc `_apply_usage_adjustment()` — projection now includes it.

**Output:** `usageAdjustment` dict in projection response with `statMultipliers` and `absentTeammates`.

---

## Feature 2: Opponent-Adaptive Stdev

**Problem:** Stdev uses flat `0.75x` shrinkage regardless of opponent. DEN (fast pace) produces wider stat distributions than CLE (grinding defense). Defense data is already fetched but only adjusts the mean.

**Integration point:** `core/nba_prep_projection.py` line 619, stdev calculation

**Formula:**
```
pace_weight = 0.25 (Poisson stats) or 0.50 (Normal stats)
pace_var_mult = 1.0 + pace_weight * (paceFactor - 1.0)
pace_var_mult = clamp(pace_var_mult, 0.88, 1.12)
proj_stdev = raw_stdev * 0.75 * pace_var_mult
```

**Changes:**
- `core/nba_prep_projection.py`: Compute `_pace_var_mult` from `opp_def["paceFactor"]` before stat loop. Apply in stdev calculation. Add `paceVarianceMult` to per-stat projection dict.

---

## Feature 3: Market-Implied Projection Delta

**Problem:** Pinnacle no-vig probs are fetched but never converted back to an implied projection for comparison.

**Math (Normal stats only):**
```
implied_proj = pin_line - stdev * NormalDist().inv_cdf(1 - pinnacle_no_vig_over)
delta = model_projection - implied_proj
```

**Changes:**
- `core/nba_prop_engine.py`: After line 207 (reference_book_meta assembly), compute `impliedProjection` and `modelMarketDelta`. Clamp input probs to [0.01, 0.99] for safety. Skip Poisson stats.
- `web/index.html` + `web/modules/analyze.js`: Display in Analyze tab metric cards.

---

## Feature 4: Intraday CLV as Real-Time Signal

**Problem:** Line movement is tracked post-settlement but never used as a pre-bet confirmation signal. +CLV bets hit 62.9% vs -CLV at 39.2%.

**Phase 1 (informational — implement first):**
- Enrich `_compute_intraday_clv()` in `nba_cli/ev_commands.py` to return `{delta, direction, magnitude, confidence, openLine, currentLine}`.
- Store full dict in signal context. Display in UI.

**Phase 2 (soft gate — after 100+ signals collected):**
- Add to `SIGNAL_SPEC`: `"intraday_clv_min": -0.5` — block signals where line moved >0.5 pts against our recommendation.

**Changes:**
- `nba_cli/ev_commands.py`: Upgrade `_compute_intraday_clv()` return format.
- `web/modules/analyze.js`: Display line movement badge.

---

## Feature 5: UI Math Visualization (Chart.js)

**Problem:** No graphical representation of the math. User wants to see distribution curves, line movement, and edge profiles visually.

**Approach:** Chart.js v4 via CDN + local fallback (`web/vendor/chart.min.js`), following the existing Alpine.js pattern.

**4 Charts:**

1. **Probability Distribution** (Analyze tab) — Normal bell curve or Poisson PMF bars. Vertical line at book line. Shaded P(over)/P(under). Shows raw vs calibrated probability overlay.

2. **Line Movement Timeline** (Analyze tab) — X: hours before tip. Y: line value. Multiple book series. Horizontal line at model projection.

3. **Edge/EV Scatter** (Picks tab) — X: edge. Y: EV%. Dot size = confidence. Color = stat. Clickable deep-links.

4. **Calibration Before/After** (Analyze tab) — Side-by-side bars showing raw model P(over) vs temperature-scaled P(over).

**New files:**
- `web/vendor/chart.min.js` — local CDN fallback
- `web/modules/charts.js` — chart builder functions with dark theme config

**Changes:**
- `web/index.html`: Add Chart.js script tag + `<canvas>` elements in Analyze and Picks tabs
- `web/modules/analyze.js`: Import and call chart renderers after prop result loads
- `web/modules/picks.js`: Import and call edge scatter after best_today loads
- `core/nba_ev_engine.py`: Expose `probOverRaw` (pre-calibration) in compute_ev response
- `server.py`: Add `/api/line_movement?player=X&stat=Y&date=Z` endpoint
- `web/styles.css`: Chart container styles, dark theme overrides

---

## Player-on-Player Matchups (Assessment)

**What we already have:**
- `get_position_vs_team()` — group-level defense multipliers (all guards vs team X), 20% weight via pvt_mults
- `get_matchup_history()` — player's historical stats vs opponent team, 20-40% weight
- `_DEF_WEIGHTS` — position-dependent defense weight tables (G/F/C x 7 stats x 5 weights)

**What we don't have:**
- Individual defender assignments (NBA API doesn't provide this)
- "Player X guarding Player Y" data

**Verdict:** Group-level approach captures ~80% of defensive signal. Individual defender data would require external paid provider. Not recommended unless ROI proves insufficient post-freeze.

---

## Dependency Graph
```
Feature 1 (Usage) ---------> independent
Feature 2 (Opp Stdev) -----> independent
Feature 3 (Market Delta) --> independent
Feature 4 (Intraday CLV) --> independent
Feature 5 (UI Charts) -----> consumes data from Features 1-4
```

## Verification (every feature)
- `python scripts/quality_gate.py --json` -> `"ok": true`
- `prop_ev "Anthony Edwards" ORL 1 pts 25.5 -110 -110 0 MIN` -> `"success": true` with `distributionMode`
- All `compute_ev()` calls include `stat=`
- Import policy: `core/` relative, `scripts/`+`nba_cli/` absolute

## Risk Mitigations
| Feature | Risk | Mitigation |
|---------|------|------------|
| Usage Integration | Backtest lookahead | Guard: only apply when `as_of_date is None` |
| Opp Stdev | Over-inflating stdev | Clamp [0.88, 1.12]; 50% weight Normal, 25% Poisson |
| Market Delta | inv_cdf at extreme probs | Clamp input to [0.01, 0.99] |
| Intraday CLV | Empty during off-hours | Return null gracefully |
| UI Charts | CDN unavailable | Local fallback in `web/vendor/` |
