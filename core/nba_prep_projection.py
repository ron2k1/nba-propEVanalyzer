#!/usr/bin/env python3
"""Projection-focused prep logic."""

import math
import os
import statistics
import time
import traceback

from .nba_data_collection import (
    API_DELAY,
    CURRENT_SEASON,
    PROJECTION_CONFIG,
    get_matchup_history,
    get_player_game_log,
    get_player_position,
    get_player_splits,
    get_position_vs_team,
    get_team_defensive_ratings,
    safe_div,
    safe_round,
)
from .nba_minutes_model import compute_minutes_multiplier
from .nba_prep_usage import compute_usage_adjustment

# ---------------------------------------------------------------------------
# Named projection constants (used in multiple places below)
# ---------------------------------------------------------------------------
# Fraction of the stdev to use as proj_stdev (post-regression shrinkage).
_PROJ_STDEV_SHRINK = 0.75

_STDEV_SHRINK_OVERRIDES = {}
for _s in ("pts", "reb", "ast", "pra"):
    _env_val = os.getenv(f"STDEV_SHRINK_{_s.upper()}")
    if _env_val is not None:
        try:
            _STDEV_SHRINK_OVERRIDES[_s] = float(_env_val)
        except ValueError:
            pass


def _get_stdev_shrink(stat):
    """Return per-stat stdev shrinkage factor; falls back to global default."""
    return _STDEV_SHRINK_OVERRIDES.get(stat, _PROJ_STDEV_SHRINK)

# When blending 30% of the book line into the model projection, the model
# receives this weight; the complementary book-line weight is 1 - this value.
_LINE_BLEND_MODEL_WEIGHT = 0.70

# Stdev reduction factor when a book-line blend is applied: sqrt(1 - blend_weight).
# Pre-computed once since math.sqrt is called at every stat iteration otherwise.
_LINE_BLEND_STDEV_FACTOR = math.sqrt(1.0 - _LINE_BLEND_MODEL_WEIGHT)

# Reference window for variance-inflation scaling (max games fetched per player).
_N_GAMES_REF = 25

# Continuous shrinkage strength: k/(k+n) blends weighted_avg toward season_avg.
# Lower k = more shrinkage (stronger pull to season avg).
# Override via SHRINK_K env var for sensitivity testing.
_SHRINK_K = int(os.getenv("SHRINK_K", "8"))


def _cclamp(val, lo, hi, counter):
    """Clamp *val* to [lo, hi]; increment counter[0] when the cap fires."""
    c = max(lo, min(hi, val))
    counter[0] += (c != val)
    return c


_DEF_WEIGHTS = {
    "G": {
        "pts": ("defPtsMult", 0.50, "defFg3mMult", 0.25, 0.25),
        "reb": ("defRebMult", 0.65, None, 0.00, 0.35),
        "ast": ("defAstMult", 0.55, "defTovMult", 0.20, 0.25),
        "stl": ("defStlMult", 0.55, "defPtsMult", 0.20, 0.25),
        "blk": ("defBlkMult", 0.55, "defRebMult", 0.20, 0.25),
        "tov": ("defTovMult", 0.55, "defAstMult", 0.20, 0.25),
        "fg3m": ("defFg3mMult", 0.70, "defPtsMult", 0.10, 0.20),
    },
    "F": {
        "pts": ("defPtsMult", 0.55, "defRebMult", 0.15, 0.30),
        "reb": ("defRebMult", 0.65, "defBlkMult", 0.10, 0.25),
        "ast": ("defAstMult", 0.55, "defTovMult", 0.20, 0.25),
        "stl": ("defStlMult", 0.55, "defPtsMult", 0.20, 0.25),
        "blk": ("defBlkMult", 0.60, "defRebMult", 0.15, 0.25),
        "tov": ("defTovMult", 0.55, "defAstMult", 0.20, 0.25),
        "fg3m": ("defFg3mMult", 0.65, "defPtsMult", 0.15, 0.20),
    },
    "C": {
        "pts": ("defPtsMult", 0.45, "defBlkMult", 0.25, 0.30),
        "reb": ("defRebMult", 0.70, "defBlkMult", 0.10, 0.20),
        "ast": ("defAstMult", 0.50, "defTovMult", 0.20, 0.30),
        "stl": ("defStlMult", 0.50, "defBlkMult", 0.20, 0.30),
        "blk": ("defBlkMult", 0.65, "defRebMult", 0.15, 0.20),
        "tov": ("defTovMult", 0.55, "defRebMult", 0.15, 0.30),
        "fg3m": ("defFg3mMult", 0.55, "defPtsMult", 0.20, 0.25),
    },
}


# Stat → defensive rank key mapping (for rank-weighted defense adjustment)
# Only stats whose ranks are actually populated by get_team_defensive_ratings().
# stl/blk/tov are omitted — their OPP_*_RANK fields don't exist in the API.
_STAT_TO_DEF_RANK = {
    "pts":  "defPtsRank",
    "reb":  "defRebRank",
    "ast":  "defAstRank",
    "fg3m": "defFg3mRank",
}


def _defense_adj(stat, opp_def, position, pvt_mults=None):
    if not opp_def:
        return 1.0

    weights = _DEF_WEIGHTS.get(position, _DEF_WEIGHTS["G"]).get(stat)
    if not weights:
        return 1.0

    pk, pw, sk, sw, pace_w = weights
    primary = opp_def.get(pk, 1.0) or 1.0
    secondary = (opp_def.get(sk, 1.0) or 1.0) if sk else 1.0
    pace = opp_def.get("paceFactor", 1.0) or 1.0
    adj = pw * primary + (sw * secondary if sk else 0.0) + pace_w * pace

    if pvt_mults:
        pvt_val = pvt_mults.get(stat)
        if pvt_val is not None:
            adj = 0.80 * adj + 0.20 * pvt_val

    # Rank-weight modulation: extreme ranks (top/bottom 5) get 120% of the
    # multiplier effect, middle ranks get 80%. Scales the distance from
    # neutral rather than replacing the multiplier.
    rank_key = _STAT_TO_DEF_RANK.get(stat)
    if rank_key:
        rank = opp_def.get(rank_key, 15) or 15
        rank_weight = 0.8 + 0.4 * abs(rank - 15) / 15
        adj = 1.0 + (adj - 1.0) * rank_weight

    lo, hi = PROJECTION_CONFIG["defense_adj"]
    return max(lo, min(hi, adj))


def _detect_role_change(rolling, logs):
    """Detect recent role change from last-3 min avg vs season min avg.

    Returns (detected: bool, delta: float, threshold: float).
    Threshold is max(3.0, season_avg * 0.15) — relative for starters,
    floored at 3 min for bench players.
    """
    season_base = float(rolling.get("min_avg_season", 0) or 0) or 1.0
    last3 = [max(0.0, float(logs[i].get("min", 0) or 0)) for i in range(min(3, len(logs)))]
    last3_avg = sum(last3) / len(last3) if last3 else season_base
    delta = last3_avg - season_base
    threshold = max(3.0, season_base * 0.15)
    detected = abs(delta) > threshold and len(logs) >= 3
    return detected, delta, threshold


def _home_away_adj(splits, stat, is_home, season_avg):
    if not splits:
        return 1.0
    loc = splits.get("home" if is_home else "away")
    overall = splits.get("overall")
    if not loc or not overall:
        return 1.0
    loc_val = loc.get(stat, 0) or 0
    base_val = overall.get(stat, 0) or season_avg
    if base_val <= 0:
        return 1.0
    lo, hi = PROJECTION_CONFIG["home_away"]
    return max(lo, min(hi, loc_val / base_val))


def _rest_adj(splits, stat, is_b2b):
    if not splits or not splits.get("restDays"):
        return 0.93 if is_b2b else 1.0

    rest_days = splits["restDays"]
    overall = splits.get("overall") or {}
    base_val = overall.get(stat, 0) or 0
    if base_val <= 0:
        return 0.93 if is_b2b else 1.0

    def _find(candidates):
        for k in candidates:
            v = rest_days.get(k)
            if v:
                return v
        return None

    if is_b2b:
        b2b = _find(["0", "0 Days Rest", "0 Day Rest"])
        if b2b:
            v = b2b.get(stat, 0) or 0
            if v > 0:
                lo, hi = PROJECTION_CONFIG["rest_b2b"]
                return max(lo, min(hi, v / base_val))
        return 0.93

    rested = _find(["2+", "3+", "2 Days Rest", "3+ Days Rest"])
    if rested:
        v = rested.get(stat, 0) or 0
        if v > 0:
            lo, hi = PROJECTION_CONFIG["rest_rested"]
            return max(lo, min(hi, v / base_val))
    return 1.0


def _matchup_adj(matchup_history, stat, season_avg):
    if not matchup_history or stat not in matchup_history:
        return 1.0
    h = matchup_history[stat]
    n, m_avg = h["games"], h["avg"]
    if season_avg <= 0 or m_avg <= 0:
        return 1.0
    w = 0.40 if n >= 5 else (0.30 if n >= 3 else 0.20)
    lo, hi = PROJECTION_CONFIG["matchup"]
    factor = max(lo, min(hi, m_avg / season_avg))
    return (1.0 - w) + w * factor


def _add_combo_projections(projections, logs, rolling):
    combos = [
        ("pra", ["pts", "reb", "ast"]),
        ("pr", ["pts", "reb"]),
        ("pa", ["pts", "ast"]),
        ("ra", ["reb", "ast"]),
    ]
    for key, parts in combos:
        if not all(p in projections for p in parts):
            continue
        proj = sum(projections[p]["projection"] for p in parts)
        conf = min(projections[p]["confidence"] for p in parts)
        vals = [g[key] for g in logs]
        s_avg = safe_round(statistics.mean(vals), 1) if vals else 0
        s_stdev = safe_round(statistics.stdev(vals), 2) if len(vals) >= 2 else 0
        # Sample-size-dependent stdev shrinkage (same as core stats in 1.5)
        _combo_n = len(vals)
        _combo_max_shrink = _get_stdev_shrink(key)
        _combo_n_scale = min(1.0, _combo_n / _N_GAMES_REF) if _combo_n > 0 else 0.0
        _combo_proj_stdev = s_stdev * (_combo_max_shrink * _combo_n_scale)
        # Diagnostic: component stdev sum for PRA calibration comparison
        _component_stdev_sum = None
        if key == "pra" and all(p in projections for p in parts):
            _comp_vars = sum(
                (projections[p].get("stdev") or 0) ** 2 for p in parts
            )
            _component_stdev_sum = safe_round(math.sqrt(_comp_vars), 2) if _comp_vars > 0 else 0
        projections[key] = {
            "projection": safe_round(proj, 1),
            "confidence": conf,
            "seasonAvg": s_avg,
            "stdev": s_stdev,
            "projStdev": max(safe_round(_combo_proj_stdev, 2), 0.5),
            "comboStdevMethod": "empirical",
            "componentStdevSum": _component_stdev_sum,
            "recentHighVariance": False,
            "last5Avg": rolling.get(f"{key}_avg5", 0),
            "last10Avg": rolling.get(f"{key}_avg10", 0),
            "median": rolling.get(f"{key}_median", 0),
            "min": rolling.get(f"{key}_min", 0),
            "max": rolling.get(f"{key}_max", 0),
        }


def _weighted_recent_average(values):
    if not values:
        return 0.0
    w_total, w_sum = 0.0, 0.0
    for i, v in enumerate(values):
        w = 3.0 if i < 5 else (2.0 if i < 10 else (1.0 if i < 20 else 0.5))
        w_sum += (v or 0.0) * w
        w_total += w
    return w_sum / w_total if w_total > 0 else 0.0


def _extract_blend_line(blend_with_line, stat_key):
    if blend_with_line is None:
        return None
    if isinstance(blend_with_line, dict):
        raw = blend_with_line.get(stat_key)
    else:
        raw = blend_with_line
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _project_minutes(logs, rolling, splits, is_home, is_b2b):
    vals = [max(0.0, float(g.get("min", 0) or 0)) for g in logs]
    if not vals:
        return {
            "projectedMinutes": 0.0,
            "seasonMinutes": 0.0,
            "weightedMinutes": 0.0,
            "adjustments": {"homeAway": 1.0, "rest": 1.0, "trend": 1.0, "combined": 1.0},
        }

    weighted_mins = _weighted_recent_average(vals)
    season_mins = rolling.get("min_avg_season", 0) or weighted_mins or 0.0
    min_stdev = float(rolling.get("min_stdev", 0) or 0.0)

    # Continuous shrinkage toward season minutes: k/(k+n) blend.
    n_mins = len(vals)
    if season_mins > 0 and n_mins > 0:
        _shrink_w = _SHRINK_K / (_SHRINK_K + n_mins)
        weighted_mins = (1 - _shrink_w) * weighted_mins + _shrink_w * season_mins

    base = 0.60 * weighted_mins + 0.40 * season_mins

    ha_adj = _home_away_adj(splits, "min", is_home, season_mins)
    rest_adj_v = _rest_adj(splits, "min", is_b2b)
    recent_mins = vals[:5]
    lo, hi = PROJECTION_CONFIG["mins_trend"]
    trend_adj = max(
        lo,
        min(hi, statistics.mean(recent_mins) / season_mins if recent_mins and season_mins > 0 else 1.0),
    )

    combined = ha_adj * rest_adj_v * trend_adj
    lo_c, hi_c = PROJECTION_CONFIG["combined"]
    combined = max(lo_c, min(hi_c, combined))
    projected = max(0.0, base * combined)

    # High-minutes soft cap: above 33 min, diminishing returns account for
    # load management, blowout rest, and foul trouble that affect stars.
    _SOFT_CAP = 33.0
    _DECAY = 0.30
    if projected > _SOFT_CAP:
        projected = _SOFT_CAP + (projected - _SOFT_CAP) * _DECAY

    return {
        "projectedMinutes": safe_round(projected, 2),
        "seasonMinutes": safe_round(season_mins, 2),
        "weightedMinutes": safe_round(weighted_mins, 2),
        "adjustments": {
            "homeAway": safe_round(ha_adj, 3),
            "rest": safe_round(rest_adj_v, 3),
            "trend": safe_round(trend_adj, 3),
            "combined": safe_round(combined, 3),
        },
    }


def compute_projection(
    player_id,
    opponent_abbr,
    is_home,
    is_b2b=False,
    season=None,
    blend_with_line=None,
    model_variant="full",
    as_of_date=None,
    minutes_multiplier=None,
    opponent_is_b2b=False,
    game_total=None,
    player_team_abbr=None,
):
    try:
        if season is None:
            season = CURRENT_SEASON

        variant = str(model_variant or "full").lower().strip()
        if variant not in {"full", "simple"}:
            return {"success": False, "error": f"Unsupported model_variant '{model_variant}'"}

        log_data = get_player_game_log(player_id, season, last_n=25, as_of_date=as_of_date)
        if not log_data.get("success") or not log_data.get("gameLogs"):
            return {"success": False, "error": "No game logs available"}
        logs = log_data["gameLogs"]
        rolling = log_data["rolling"]

        time.sleep(API_DELAY)
        splits_data = get_player_splits(player_id, season, as_of_date=as_of_date)
        splits = splits_data.get("splits", {}) if splits_data.get("success") else {}

        defense_data = get_team_defensive_ratings(as_of_date=as_of_date)
        opp_def = None
        if defense_data.get("success"):
            for t in defense_data.get("teams", []):
                if t.get("abbreviation") == opponent_abbr:
                    opp_def = t
                    break

        time.sleep(API_DELAY)
        pos_info = get_player_position(player_id)
        position = pos_info.get("position", "G")

        matchup_history = get_matchup_history(logs, opponent_abbr) if variant == "full" else None
        pvt_mults = None
        if variant == "full" and opp_def:
            opp_team_id = opp_def.get("teamId")
            if opp_team_id:
                pvt_data = get_position_vs_team(opp_team_id, season, as_of_date=as_of_date)
                if pvt_data.get("success"):
                    pvt_mults = pvt_data.get("multipliers")

        # Feature 1: Usage rate integration — LIVE MODE ONLY.
        # get_team_roster_status uses LeagueDashPlayerStats(last_n_games=5)
        # which returns the last 5 games from *now*, not from as_of_date.
        # Until we have historical roster snapshots, running this in backtests
        # would be lookahead. The `if player_team_abbr:` guard keeps it
        # live-only since the backtest caller does not pass that argument.
        # Computed here so roster_context is available for
        # compute_minutes_multiplier below.
        #
        # Uses compute_usage_adjustment_with_news() so that news-reported
        # "Out" / "Doubtful" teammates are reclassified to "Likely Inactive"
        # BEFORE the full USG redistribution runs.  Falls back to the base
        # compute_usage_adjustment() if news fetch fails (the _with_news
        # function handles that internally).
        _usage_mults = {}
        _usage_ctx = None
        _star_replacement = False
        if player_team_abbr:
            try:
                from .nba_injury_news import compute_usage_adjustment_with_news
                _usg = compute_usage_adjustment_with_news(
                    player_id, player_team_abbr, season
                )
                if _usg.get("success") and _usg.get("absentTeammates"):
                    _usage_mults = _usg.get("statMultipliers") or {}
                    _usage_ctx = _usg
                    # Star replacement detection: cap was hit AND the biggest
                    # absent teammate had >= 2x the target player's usage rate.
                    if _usg.get("capHit") and \
                       (_usg.get("maxAbsentUsg", 0) >= 2 * _usg.get("targetUsgPct", 100)):
                        _star_replacement = True
                elif not _usg.get("success"):
                    import logging
                    logging.getLogger("nba_engine.projection").warning(
                        "USG adjustment returned success=False for %s/%s: %s",
                        player_id, player_team_abbr, _usg.get("error", "unknown"),
                    )
            except Exception as _usg_err:
                import logging
                logging.getLogger("nba_engine.projection").warning(
                    "USG adjustment failed for player_id=%s team=%s: %s",
                    player_id, player_team_abbr, _usg_err,
                )

        minutes_ctx = _project_minutes(logs, rolling, splits, is_home, is_b2b)
        base_projected_minutes = minutes_ctx["projectedMinutes"] or 0.0

        # --- Minutes model: enrich minutesProjection with confidence + reasoning ---
        _excluded = log_data.get("excludedGames") or []
        _mm = compute_minutes_multiplier(rolling, logs, is_b2b=is_b2b, splits=splits,
                                         excluded_games=_excluded,
                                         roster_context=_usage_ctx)
        minutes_ctx["minutesMultiplier"]  = _mm["multiplier"]
        minutes_ctx["minutesConfidence"]  = _mm["minutesConfidence"]
        minutes_ctx["minutesReasoning"]   = _mm["minutesReasoning"]
        minutes_ctx["minutesCapApplied"]  = any(
            "injury_return" in r for r in _mm["minutesReasoning"]
        )
        minutes_ctx["minutesCapReason"]   = next(
            (r for r in _mm["minutesReasoning"] if "injury_return" in r), None
        )

        # Effective multiplier: external caller override takes precedence over model
        if minutes_multiplier is not None:
            _eff_mult = max(0.50, min(2.0, float(minutes_multiplier)))
            minutes_ctx["externalMultiplier"] = safe_round(_eff_mult, 3)
            minutes_ctx["minutesReasoning"].append(f"external_override:{_eff_mult:.2f}")
        else:
            _eff_mult = _mm["multiplier"]

        projected_minutes = max(0.0, base_projected_minutes * _eff_mult)
        minutes_ctx["projectedMinutes"] = safe_round(projected_minutes, 2)

        # --- Pre-loop: derived context signals (computed once, used per-stat) ---

        # Gap 8.15: Recent role change
        _role_change_detected, _role_change_delta, _ = _detect_role_change(rolling, logs)

        # Gap 8.17: Post-blowout urgency — player plus/minus as blowout proxy
        # Player on court during a blowout will have a large +/-; this correlates
        # with next-game motivation/urgency (prior loss) or coasting (prior win).
        _prior_plusminus = float(logs[1].get("plusMinus", 0) or 0) if len(logs) >= 2 else 0.0
        _blowout_adj_pts_ast = 1.0
        if _prior_plusminus <= -20:
            _blowout_adj_pts_ast = 1.04   # urgency after blowout loss
        elif _prior_plusminus >= 25:
            _blowout_adj_pts_ast = 0.97   # coasting after blowout win

        # Gap 8.19: Denver altitude boost — visiting player only
        # Ball Arena (5,280 ft) produces measurable fatigue for visiting defenses;
        # visiting players historically score ~2.5% more in Denver home games.
        # DEN home players already have this baked into their season averages.
        _altitude_mult = 1.025 if (not is_home and str(opponent_abbr or "").upper() == "DEN") else 1.0

        # Gap 8.22: Rest advantage interaction term
        # Well-rested player (not B2B) vs fatigued opponent (B2B) = compounding edge.
        # The individual effects of player rest and opponent B2B are modeled separately;
        # this adds the interaction: both conditions true → extra +2.5% on pts/ast.
        _rest_advantage = (not is_b2b) and opponent_is_b2b

        # Gap 8.18: 3-in-4 nights detection
        # If the player played both 3 days ago AND 1 day ago (relative to logs[0]),
        # they are on a 3rd game in 4 nights → fatigue compounds beyond simple B2B.
        _is_3in4 = False
        if is_b2b and len(logs) >= 2:
            def _parse_game_date(raw):
                s = str(raw or "").strip()
                from datetime import datetime as _dt
                for _fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
                    try:
                        return _dt.strptime(s, _fmt).date()
                    except ValueError:
                        continue
                return None
            try:
                from datetime import timedelta as _td
                _today_date = _parse_game_date(logs[0].get("gameDate", ""))
                if _today_date:
                    # logs[0] = D-1 (last night). D-1 - 2 = D-3.
                    # 3-in-4: played D-3, D-1, and tonight (D) → timedelta(2).
                    _target_d3 = _today_date - _td(days=2)
                    _is_3in4 = any(
                        _parse_game_date(g.get("gameDate", "")) == _target_d3
                        for g in logs[2:5]  # only look back a few games
                    )
            except (ValueError, TypeError):
                _is_3in4 = False

        # Gap 8.27: High-minutes previous game fatigue
        # Playing ≥38 min the night before taxes recovery; next-game pts/ast
        # depressed ~3%, reb ~2%. Effect is independent of B2B — even a
        # well-rested player who logged 42 min 2 nights ago is fatigued.
        # Threshold 38 min chosen as top-10% of starter distributions.
        _prior_mins = float(logs[0].get("min", 0) or 0) if logs else 0.0
        _high_mins_fatigue = _prior_mins >= 38.0

        # Gap 8.30: Road trip accumulation fatigue
        # 3+ consecutive away games = travel fatigue (hotel, timezone, no
        # home practice facilities). Count tonight + recent away streak.
        # Only fires when tonight is also away (is_home=False).
        _road_trip_len = 0
        if not is_home:
            _road_trip_len = 1  # tonight is away
            for _rg in logs[:7]:
                if not _rg.get("isHome", True):
                    _road_trip_len += 1
                else:
                    break
        _road_trip_fatigue = _road_trip_len >= 3

        # Feature 2: Opponent-adaptive stdev — pace-based variance multiplier.
        # Fast-paced opponents (DEN) widen stat distributions; grinding defenses (CLE) narrow them.
        _POISSON_PACE_WEIGHT = 0.25
        _NORMAL_PACE_WEIGHT = 0.50
        _opp_pace_factor = (opp_def.get("paceFactor", 1.0) or 1.0) if opp_def else 1.0

        core_stats = ["pts", "reb", "ast", "stl", "blk", "tov", "fg3m"]
        projections = {}

        for stat in core_stats:
            vals = [float(g.get(stat, 0) or 0) for g in logs]
            mins = [max(0.0, float(g.get("min", 0) or 0)) for g in logs]
            if not vals:
                continue

            n = len(vals)
            weighted_avg = _weighted_recent_average(vals)
            weighted_mins = _weighted_recent_average(mins) or 1.0
            season_avg = float(rolling.get(f"{stat}_avg_season", 0) or 0.0)
            season_mins = float(rolling.get("min_avg_season", 0) or weighted_mins or 1.0)
            stdev_val = float(rolling.get(f"{stat}_stdev", 0) or 0.0)

            # Gap 8.14: Home/Away stat split calibration
            # Replace season_avg with location-specific avg when:
            #   - split has ≥8 games (sufficient sample)
            #   - split differs from season avg by >8% (avoids noise for stable players)
            # Impact: players with large home/away splits (e.g., +10% away scorer)
            # get more accurate base projections. Blending and per-min rate still apply.
            _loc_key = "home" if is_home else "away"
            _split_data = (splits or {}).get(_loc_key, {})
            _split_val = _split_data.get(stat)
            _split_gp = int(_split_data.get("gp", 0) or 0)
            _home_away_split_used = False
            if (
                _split_val is not None
                and _split_gp >= 8
                and season_avg > 0
                and abs(float(_split_val) - season_avg) / season_avg > 0.08
            ):
                season_avg = float(_split_val)
                _home_away_split_used = True

            # Gap 8.15: Role change — bias projection base to last-5 window.
            # Rebind season_avg and season_mins locally so regression-to-mean
            # targets the recent role, not the stale season-wide average.
            _stat_role_change = _role_change_detected
            if _stat_role_change and len(vals) >= 3:
                _last5_vals = vals[:min(5, len(vals))]
                _last5_mins = [max(0.0, float(logs[i].get("min", 0) or 0)) for i in range(len(_last5_vals))]
                season_avg = sum(_last5_vals) / len(_last5_vals)
                season_mins = (sum(_last5_mins) / len(_last5_mins)) if _last5_mins else season_mins

            # Continuous shrinkage toward season average: k/(k+n) blend.
            # Lower k = stronger pull to season avg; n = games in window.
            _shrink_w = _SHRINK_K / (_SHRINK_K + n) if n > 0 else 1.0
            if season_avg > 0 and n > 0:
                weighted_avg = (1 - _shrink_w) * weighted_avg + _shrink_w * season_avg

            # Variance inflation for small samples: widen CDF tails when we
            # have fewer games, reflecting higher uncertainty.  No effect at
            # n >= 25 (our max fetch window).
            if n < _N_GAMES_REF and n > 0:
                stdev_val = stdev_val * (1 + 2.0 * (1.0 / math.sqrt(n) - 1.0 / math.sqrt(_N_GAMES_REF)))

            weighted_rate = safe_div(weighted_avg, weighted_mins, default=0.0)
            season_rate = safe_div(season_avg, season_mins, default=0.0)
            per_min_rate = max(0.0, 0.60 * weighted_rate + 0.40 * season_rate)

            def_adj_v = _defense_adj(stat, opp_def, position, pvt_mults if variant == "full" else None)
            matchup_v = _matchup_adj(matchup_history, stat, season_avg) if variant == "full" else 1.0
            stat_mult = def_adj_v * matchup_v
            lo, hi = PROJECTION_CONFIG["combined"]
            stat_mult = max(lo, min(hi, stat_mult))
            _cap_hits = [0]

            # Opponent B2B pace adjustment (Phase 3b):
            # Opponent on B2B → faster pace, less rested defense → slight boost for pts/reb/ast.
            # Net effect: +1.5% at 50% weight = +0.75% boost; capped by combined range.
            if opponent_is_b2b and stat in ("pts", "reb", "ast"):
                stat_mult = _cclamp(stat_mult * 1.015, lo, hi, _cap_hits)

            # Gap 8.18: 3-in-4 nights — compounded fatigue (1.5× normal B2B effect).
            # Normal B2B applies 0.93 rest multiplier. 3-in-4 adds an extra 0.96× on top.
            if _is_3in4 and stat in ("pts", "reb", "ast"):
                stat_mult = _cclamp(stat_mult * 0.96, lo, hi, _cap_hits)

            # Game total pace adjustment (Phase 4a):
            # Season avg total ≈ 226. Per 5-point deviation: ±0.75% pts at 50% weight.
            # Applied to pts only; capped at ±7%.
            if game_total is not None and stat == "pts":
                _total_deviation = (float(game_total) - 226.0) / 5.0
                _total_mult = 1.0 + _total_deviation * 0.0075
                _total_mult = max(0.93, min(1.07, _total_mult))
                stat_mult = _cclamp(stat_mult * _total_mult, lo, hi, _cap_hits)

            # Gap 8.17: Post-blowout urgency (pts/ast only)
            if stat in ("pts", "ast") and _blowout_adj_pts_ast != 1.0:
                stat_mult = _cclamp(stat_mult * _blowout_adj_pts_ast, lo, hi, _cap_hits)

            # Gap 8.19: Denver altitude — visiting player boost (pts/ast only)
            if stat in ("pts", "ast") and _altitude_mult != 1.0:
                stat_mult = _cclamp(stat_mult * _altitude_mult, lo, hi, _cap_hits)

            # Gap 8.22: Rest advantage interaction (player rested × opponent B2B)
            if stat in ("pts", "ast") and _rest_advantage:
                stat_mult = _cclamp(stat_mult * 1.025, lo, hi, _cap_hits)

            # Gap 8.23: Hot/cold streak persistence (pts/ast only, need ≥8 logs)
            # High-usage scorers in hot streaks (≥6 of last 8 over) continue at 56-60%.
            # Cold streaks (≤2 of last 8 over) mean-revert; apply small fade.
            _streak_mult = 1.0
            if stat in ("pts", "ast") and len(vals) >= 8 and season_avg > 0:
                _over_count_l8 = sum(1 for v in vals[:8] if v >= season_avg)
                _over_rate_l8 = _over_count_l8 / 8.0
                if _over_rate_l8 >= 0.75:    # ≥6/8: hot streak — continue
                    _streak_mult = 1.03
                elif _over_rate_l8 <= 0.25:  # ≤2/8: cold streak — mean revert
                    _streak_mult = 0.98
                if _streak_mult != 1.0:
                    stat_mult = _cclamp(stat_mult * _streak_mult, lo, hi, _cap_hits)

            # Gap 8.27: High-minutes fatigue (≥38 min previous game)
            # pts/ast: -3% (shot quality and decision-making); reb: -2% (boxing out energy).
            if _high_mins_fatigue:
                if stat in ("pts", "ast"):
                    stat_mult = _cclamp(stat_mult * 0.97, lo, hi, _cap_hits)
                elif stat == "reb":
                    stat_mult = _cclamp(stat_mult * 0.98, lo, hi, _cap_hits)

            # Gap 8.30: Road trip fatigue (≥3 consecutive away games including tonight)
            # Travel accumulation depresses pts/reb/ast by ~2%.
            if _road_trip_fatigue and stat in ("pts", "reb", "ast"):
                stat_mult = _cclamp(stat_mult * 0.98, lo, hi, _cap_hits)

            model_projection = max(0.0, projected_minutes * per_min_rate * stat_mult)

            # Feature 1: Apply usage rate multiplier to model projection.
            _stat_usage_mult = _usage_mults.get(stat, 1.0) if _usage_mults else 1.0
            if _stat_usage_mult != 1.0:
                model_projection *= _stat_usage_mult

            blend_line = _extract_blend_line(blend_with_line, stat)
            if blend_line is not None:
                final_projection = max(
                    0.0,
                    _LINE_BLEND_MODEL_WEIGHT * model_projection + (1.0 - _LINE_BLEND_MODEL_WEIGHT) * blend_line,
                )
            else:
                final_projection = model_projection

            # --- #5: Recent high-variance detection (role instability signal) ---
            # If the player's last-5 stdev exceeds 1.5× the full-window stdev,
            # the player's role/usage is unstable — books are better calibrated
            # than the model in this case. Flag and widen proj_stdev by 1.2×.
            _recent_high_variance = False
            if len(vals) >= 3 and stdev_val > 0:
                _last5 = vals[:min(5, len(vals))]
                _l5_mean = sum(_last5) / len(_last5)
                _l5_stdev = math.sqrt(
                    sum((v - _l5_mean) ** 2 for v in _last5) / max(len(_last5) - 1, 1)
                )
                if _l5_stdev > 1.5 * stdev_val:
                    _recent_high_variance = True

            # --- #1: Post-regression stdev shrinkage (sample-size-dependent) ---
            # The weighted average already regresses toward the season mean,
            # resolving ~30-40% of raw outcome variance. proj_stdev should
            # represent projection error, not raw outcome variance.
            # Shrinkage scales with sample size: at n=5 → 0.15×, at n=25 → 0.75×.
            # Env override (STDEV_SHRINK_<STAT>) sets the max factor at full sample.
            _stdev_max_shrink = _get_stdev_shrink(stat)
            _stdev_n_scale = min(1.0, n / _N_GAMES_REF) if n > 0 else 0.0
            _proj_stdev = stdev_val * (_stdev_max_shrink * _stdev_n_scale)

            # Feature 2: Pace-based variance scaling.
            _pace_w = _POISSON_PACE_WEIGHT if stat in ("stl", "blk", "fg3m", "tov") else _NORMAL_PACE_WEIGHT
            _pace_var_mult = max(0.88, min(1.12, 1.0 + _pace_w * (_opp_pace_factor - 1.0)))
            _proj_stdev *= _pace_var_mult

            # --- #2: Line-blend stdev reduction ---
            # Blending (1 - _LINE_BLEND_MODEL_WEIGHT) of the book line anchors that
            # fraction of projection uncertainty to the market consensus. The effective
            # CDF spread is stdev * sqrt(_LINE_BLEND_MODEL_WEIGHT) ≈ 0.837×.
            if blend_line is not None:
                _proj_stdev *= _LINE_BLEND_STDEV_FACTOR

            # --- #5 cont.: Inflate proj_stdev for recent high-variance players ---
            if _recent_high_variance:
                _proj_stdev *= 1.20

            proj_stdev = max(safe_round(_proj_stdev, 2), 0.5)
            cv = stdev_val / season_avg if season_avg > 0 else 1.0
            sample_conf = min(1.0, n / 20)
            consist_conf = max(0.0, 1.0 - cv)
            confidence = safe_round(max(0.10, min(0.99, 0.5 * sample_conf + 0.5 * consist_conf)), 2)

            projections[stat] = {
                "projection": safe_round(final_projection, 1),
                "projectionModel": safe_round(model_projection, 1),
                "projectionPreBlend": safe_round(model_projection, 1),
                "blendLine": safe_round(blend_line, 3) if blend_line is not None else None,
                "confidence": confidence,
                "seasonAvg": safe_round(season_avg, 2),
                "weightedAvg": safe_round(weighted_avg, 2),
                "nGames": n,
                "shrinkWeight": safe_round(_shrink_w, 4),
                "last5Avg": rolling.get(f"{stat}_avg5", 0),
                "last10Avg": rolling.get(f"{stat}_avg10", 0),
                "median": rolling.get(f"{stat}_median", 0),
                "stdev": safe_round(stdev_val, 2),
                "projStdev": proj_stdev,
                "recentHighVariance": _recent_high_variance,
                "homeAwaySplitUsed": _home_away_split_used,
                "recentRoleChange": _stat_role_change,
                "roleChangeDelta": safe_round(_role_change_delta, 1) if _stat_role_change else None,
                "blowoutAdj": (
                    safe_round(_blowout_adj_pts_ast, 3)
                    if stat in ("pts", "ast") and _blowout_adj_pts_ast != 1.0
                    else None
                ),
                "altitudeAdj": True if stat in ("pts", "ast") and _altitude_mult != 1.0 else None,
                "restAdvantageAdj": True if stat in ("pts", "ast") and _rest_advantage else None,
                "streakMult": safe_round(_streak_mult, 3) if stat in ("pts", "ast") and _streak_mult != 1.0 else None,
                "highMinsFatigueAdj": True if _high_mins_fatigue and stat in ("pts", "ast", "reb") else None,
                "roadTripFatigueAdj": True if _road_trip_fatigue and stat in ("pts", "reb", "ast") else None,
                "usageMult": safe_round(_stat_usage_mult, 3) if _stat_usage_mult != 1.0 else None,
                "paceVarianceMult": safe_round(_pace_var_mult, 3) if _pace_var_mult != 1.0 else None,
                "capHitCount": _cap_hits[0],
                "min": rolling.get(f"{stat}_min", 0),
                "max": rolling.get(f"{stat}_max", 0),
                "perMinRate": safe_round(per_min_rate, 4),
                "projectedMinutes": safe_round(projected_minutes, 2),
                "adjustments": {
                    "defense": safe_round(def_adj_v, 3),
                    "matchup": safe_round(matchup_v, 3),
                    "statMultiplier": safe_round(stat_mult, 3),
                    "minutes": minutes_ctx.get("adjustments", {}),
                },
            }

        _add_combo_projections(projections, logs, rolling)

        for combo_stat in ("pra", "pr", "pa", "ra"):
            if combo_stat not in projections:
                continue
            blend_line = _extract_blend_line(blend_with_line, combo_stat)
            if blend_line is None:
                continue
            model_projection = float(projections[combo_stat]["projection"])
            final_projection = max(
                0.0,
                _LINE_BLEND_MODEL_WEIGHT * model_projection + (1.0 - _LINE_BLEND_MODEL_WEIGHT) * blend_line,
            )
            projections[combo_stat]["projectionModel"] = safe_round(model_projection, 1)
            projections[combo_stat]["projectionPreBlend"] = safe_round(model_projection, 1)
            projections[combo_stat]["projection"] = safe_round(final_projection, 1)
            projections[combo_stat]["blendLine"] = safe_round(blend_line, 3)
            # #2: line-blend stdev reduction for combo stats
            _cs = projections[combo_stat].get("projStdev") or 0
            if _cs > 0:
                projections[combo_stat]["projStdev"] = max(safe_round(_cs * _LINE_BLEND_STDEV_FACTOR, 2), 0.5)

        opp_context = None
        if opp_def:
            opp_context = {
                "abbreviation": opp_def.get("abbreviation"),
                "defRtg": opp_def.get("defRtg"),
                "pace": opp_def.get("pace"),
                "paceFactor": opp_def.get("paceFactor"),
                "defPtsMult": opp_def.get("defPtsMult"),
                "defRebMult": opp_def.get("defRebMult"),
                "defAstMult": opp_def.get("defAstMult"),
                "defFg3mMult": opp_def.get("defFg3mMult"),
                "defBlkMult": opp_def.get("defBlkMult"),
                "defTovMult": opp_def.get("defTovMult"),
                "defPtsRank": opp_def.get("defPtsRank"),
            }

        return {
            "success": True,
            "projections": projections,
            "gameLogs": logs,
            "rolling": rolling,
            "hitRates": log_data.get("hitRates"),
            "playerId": player_id,
            "opponent": opponent_abbr,
            "isHome": is_home,
            "isB2B": is_b2b,
            "is3in4": _is_3in4,
            "priorMins": safe_round(_prior_mins, 1),
            "highMinsFatigue": _high_mins_fatigue,
            "roadTripLen": _road_trip_len,
            "roadTripFatigue": _road_trip_fatigue,
            "position": position,
            "gamesPlayed": len(logs),
            "matchupHistory": matchup_history,
            "opponentDefense": opp_context,
            "modelVariant": variant,
            "asOfDate": str(as_of_date) if as_of_date is not None else None,
            "minutesProjection": minutes_ctx,
            "usageAdjustment": _usage_ctx,
            "starReplacementFlag": _star_replacement,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}


def compute_projection_simple(
    player_id,
    opponent_abbr,
    is_home,
    is_b2b=False,
    season=None,
    blend_with_line=None,
    as_of_date=None,
    opponent_is_b2b=False,
    game_total=None,
    player_team_abbr=None,
):
    return compute_projection(
        player_id=player_id,
        opponent_abbr=opponent_abbr,
        is_home=is_home,
        is_b2b=is_b2b,
        season=season,
        blend_with_line=blend_with_line,
        model_variant="simple",
        as_of_date=as_of_date,
        opponent_is_b2b=opponent_is_b2b,
        game_total=game_total,
        player_team_abbr=player_team_abbr,
    )
