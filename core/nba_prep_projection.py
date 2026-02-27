#!/usr/bin/env python3
"""Projection-focused prep logic."""

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

    lo, hi = PROJECTION_CONFIG["defense_adj"]
    return max(lo, min(hi, adj))


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
        projections[key] = {
            "projection": safe_round(proj, 1),
            "confidence": conf,
            "seasonAvg": s_avg,
            "stdev": s_stdev,
            "projStdev": s_stdev,
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

        minutes_ctx = _project_minutes(logs, rolling, splits, is_home, is_b2b)
        base_projected_minutes = minutes_ctx["projectedMinutes"] or 0.0

        # --- Minutes model: enrich minutesProjection with confidence + reasoning ---
        _mm = compute_minutes_multiplier(rolling, logs, is_b2b=is_b2b, splits=splits)
        minutes_ctx["minutesMultiplier"]  = _mm["multiplier"]
        minutes_ctx["minutesConfidence"]  = _mm["minutesConfidence"]
        minutes_ctx["minutesReasoning"]   = _mm["minutesReasoning"]

        # Effective multiplier: external caller override takes precedence over model
        if minutes_multiplier is not None:
            _eff_mult = max(0.50, min(2.0, float(minutes_multiplier)))
            minutes_ctx["externalMultiplier"] = safe_round(_eff_mult, 3)
            minutes_ctx["minutesReasoning"].append(f"external_override:{_eff_mult:.2f}")
        else:
            _eff_mult = _mm["multiplier"]

        projected_minutes = max(0.0, base_projected_minutes * _eff_mult)
        minutes_ctx["projectedMinutes"] = safe_round(projected_minutes, 2)

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

            weighted_rate = safe_div(weighted_avg, weighted_mins, default=0.0)
            season_rate = safe_div(season_avg, season_mins, default=0.0)
            per_min_rate = max(0.0, 0.60 * weighted_rate + 0.40 * season_rate)

            def_adj_v = _defense_adj(stat, opp_def, position, pvt_mults if variant == "full" else None)
            matchup_v = _matchup_adj(matchup_history, stat, season_avg) if variant == "full" else 1.0
            stat_mult = def_adj_v * matchup_v
            lo, hi = PROJECTION_CONFIG["combined"]
            stat_mult = max(lo, min(hi, stat_mult))

            model_projection = max(0.0, projected_minutes * per_min_rate * stat_mult)
            blend_line = _extract_blend_line(blend_with_line, stat)
            final_projection = max(0.0, 0.70 * model_projection + 0.30 * blend_line) if blend_line is not None else model_projection

            proj_stdev = safe_round(stdev_val, 2)
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
                "last5Avg": rolling.get(f"{stat}_avg5", 0),
                "last10Avg": rolling.get(f"{stat}_avg10", 0),
                "median": rolling.get(f"{stat}_median", 0),
                "stdev": safe_round(stdev_val, 2),
                "projStdev": proj_stdev,
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
            final_projection = max(0.0, 0.70 * model_projection + 0.30 * blend_line)
            projections[combo_stat]["projectionModel"] = safe_round(model_projection, 1)
            projections[combo_stat]["projectionPreBlend"] = safe_round(model_projection, 1)
            projections[combo_stat]["projection"] = safe_round(final_projection, 1)
            projections[combo_stat]["blendLine"] = safe_round(blend_line, 3)

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
            "position": position,
            "gamesPlayed": len(logs),
            "matchupHistory": matchup_history,
            "opponentDefense": opp_context,
            "modelVariant": variant,
            "asOfDate": str(as_of_date) if as_of_date is not None else None,
            "minutesProjection": minutes_ctx,
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
    )
