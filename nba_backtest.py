#!/usr/bin/env python3
"""Historical backtesting utilities for projection and EV calibration."""

import math
import time
from datetime import datetime, timedelta

from nba_api.stats.endpoints import boxscoretraditionalv3, scoreboardv3

from nba_data_collection import (
    API_DELAY,
    HEADERS,
    PROJECTION_CONFIG,
    retry_api_call,
    safe_round,
)
from nba_data_prep import compute_projection, compute_projection_simple
from nba_model_training import compute_ev

TRACKED_STATS = ["pts", "reb", "ast", "fg3m", "pra", "stl", "blk", "tov"]
DEFAULT_SYNTHETIC_OVER_ODDS = -110
DEFAULT_SYNTHETIC_UNDER_ODDS = -110


def _parse_date(value):
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _season_from_date(date_obj):
    y = date_obj.year
    return f"{y}-{str(y + 1)[-2:]}" if date_obj.month >= 10 else f"{y - 1}-{str(y)[-2:]}"


def _iter_dates(date_from, date_to):
    cur = date_from
    while cur <= date_to:
        yield cur
        cur += timedelta(days=1)


def _fetch_games_for_date(date_obj):
    date_str = date_obj.strftime("%Y-%m-%d")

    def fetch():
        return scoreboardv3.ScoreboardV3(
            game_date=date_str,
            league_id="00",
            timeout=30,
        ).get_dict()

    data = retry_api_call(fetch)
    raw_games = data.get("scoreboard", {}).get("games", []) or []
    games = []
    for g in raw_games:
        home = g.get("homeTeam", {}) or {}
        away = g.get("awayTeam", {}) or {}
        home_id = int(home.get("teamId", 0) or 0)
        away_id = int(away.get("teamId", 0) or 0)
        if home_id <= 0 or away_id <= 0:
            continue
        games.append(
            {
                "gameId": str(g.get("gameId", "") or ""),
                "homeTeamId": home_id,
                "awayTeamId": away_id,
                "homeAbbr": str(home.get("teamTricode", "") or "").upper(),
                "awayAbbr": str(away.get("teamTricode", "") or "").upper(),
            }
        )
    return games


def _teams_played_on_date(date_obj, cache):
    key = date_obj.isoformat()
    if key in cache:
        return cache[key]
    try:
        games = _fetch_games_for_date(date_obj)
        team_ids = set()
        for g in games:
            team_ids.add(g["homeTeamId"])
            team_ids.add(g["awayTeamId"])
        cache[key] = team_ids
        return team_ids
    except Exception:
        cache[key] = set()
        return set()


def _minutes_played(raw_min):
    s = str(raw_min or "").strip()
    if not s:
        return 0.0
    if s.upper().startswith("DNP"):
        return 0.0
    if ":" in s:
        try:
            mm, ss = s.split(":", 1)
            return max(0.0, float(mm) + float(ss) / 60.0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return max(0.0, float(s))
    except (TypeError, ValueError):
        return 0.0


def _fetch_boxscore_players(game_id):
    def fetch():
        return boxscoretraditionalv3.BoxScoreTraditionalV3(
            game_id=game_id,
            timeout=30,
        )

    payload = retry_api_call(fetch).get_dict() or {}
    box = payload.get("boxScoreTraditional", {}) or {}
    rows = []
    for team_key in ("homeTeam", "awayTeam"):
        team = box.get(team_key, {}) or {}
        team_id = int(team.get("teamId", 0) or 0)
        for player in team.get("players", []) or []:
            pid = int(player.get("personId", 0) or 0)
            stats = player.get("statistics", {}) or {}
            rows.append(
                {
                    "PLAYER_ID": pid,
                    "TEAM_ID": team_id,
                    "MIN": stats.get("minutes"),
                    "PTS": stats.get("points"),
                    "REB": stats.get("reboundsTotal"),
                    "AST": stats.get("assists"),
                    "STL": stats.get("steals"),
                    "BLK": stats.get("blocks"),
                    "TOV": stats.get("turnovers"),
                    "FG3M": stats.get("threePointersMade"),
                }
            )
    return rows


def _actual_stats(row):
    pts = float(row.get("PTS", 0) or 0.0)
    reb = float(row.get("REB", 0) or 0.0)
    ast = float(row.get("AST", 0) or 0.0)
    stl = float(row.get("STL", 0) or 0.0)
    blk = float(row.get("BLK", 0) or 0.0)
    tov = float(row.get("TO", row.get("TOV", 0)) or 0.0)
    fg3m = float(row.get("FG3M", 0) or 0.0)
    return {
        "pts": pts,
        "reb": reb,
        "ast": ast,
        "fg3m": fg3m,
        "stl": stl,
        "blk": blk,
        "tov": tov,
        "pra": pts + reb + ast,
    }


def _synthetic_line(proj_stat):
    baseline = float(proj_stat.get("seasonAvg") or proj_stat.get("projection") or 0.0)
    return max(0.5, math.floor(baseline) + 0.5)


def _grade_side(actual, line, side):
    if abs(float(actual) - float(line)) < 1e-9:
        return "push"
    if side == "over":
        return "win" if float(actual) > float(line) else "loss"
    return "win" if float(actual) < float(line) else "loss"


def _pnl_for_american(result, american_odds):
    o = float(american_odds)
    if result == "push":
        return 0.0
    if result == "loss":
        return -1.0
    if o > 0:
        return o / 100.0
    return 100.0 / abs(o)


def _new_accumulator():
    return {
        "sampleCount": 0,
        "projectionCalls": 0,
        "projectionErrors": 0,
        "samplesByStat": {s: 0 for s in TRACKED_STATS},
        "maeSumByStat": {s: 0.0 for s in TRACKED_STATS},
        "brierSumByStat": {s: 0.0 for s in TRACKED_STATS},
        "calibrationByStat": {
            s: {
                i: {"count": 0, "predOverSum": 0.0, "actualOverHits": 0.0}
                for i in range(10)
            }
            for s in TRACKED_STATS
        },
        "roiSimulation": {
            "betsPlaced": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "pnlUnits": 0.0,
        },
    }


def _finalize_accumulator(acc):
    mae = {}
    brier = {}
    calibration = {}
    for stat in TRACKED_STATS:
        n = acc["samplesByStat"][stat]
        mae[stat] = safe_round(acc["maeSumByStat"][stat] / n, 4) if n > 0 else None
        brier[stat] = safe_round(acc["brierSumByStat"][stat] / n, 4) if n > 0 else None
        bins_out = []
        for i in range(10):
            b = acc["calibrationByStat"][stat][i]
            c = b["count"]
            lo = i * 10
            hi = (i + 1) * 10
            bins_out.append(
                {
                    "bin": f"{lo}-{hi}%",
                    "count": c,
                    "avgPredOverProbPct": safe_round((b["predOverSum"] / c) * 100.0, 2) if c > 0 else None,
                    "actualOverHitRatePct": safe_round((b["actualOverHits"] / c) * 100.0, 2) if c > 0 else None,
                }
            )
        calibration[stat] = bins_out

    roi = acc["roiSimulation"]
    bets = roi["betsPlaced"]
    roi["pnlUnits"] = safe_round(roi["pnlUnits"], 4)
    roi["roiPctPerBet"] = safe_round((roi["pnlUnits"] / bets) * 100.0, 3) if bets > 0 else None
    roi["hitRatePct"] = safe_round((roi["wins"] / bets) * 100.0, 3) if bets > 0 else None

    return {
        "sampleCount": acc["sampleCount"],
        "projectionCalls": acc["projectionCalls"],
        "projectionErrors": acc["projectionErrors"],
        "maeByStat": mae,
        "brierByStat": brier,
        "calibrationByStat": calibration,
        "roiSimulation": roi,
    }


def _model_callable(model_name):
    if model_name == "simple":
        return compute_projection_simple
    return compute_projection


def run_backtest(date_from, date_to=None, model="both"):
    """
    Backtest projection + EV quality for one day or a date range.
    """
    start = _parse_date(date_from)
    end = _parse_date(date_to) if date_to else start
    if not start:
        return {"success": False, "error": "Invalid date_from. Use YYYY-MM-DD."}
    if not end:
        return {"success": False, "error": "Invalid date_to. Use YYYY-MM-DD."}
    if end < start:
        return {"success": False, "error": "date_to must be >= date_from."}

    model_key = str(model or "both").lower().strip()
    if model_key not in {"full", "simple", "both"}:
        return {"success": False, "error": "model must be one of: full, simple, both"}

    models = ["full", "simple"] if model_key == "both" else [model_key]
    accumulators = {m: _new_accumulator() for m in models}
    projection_cache = {}
    schedule_cache = {}

    try:
        for day in _iter_dates(start, end):
            games = _fetch_games_for_date(day)
            if not games:
                continue
            b2b_team_ids = _teams_played_on_date(day - timedelta(days=1), schedule_cache)
            season = _season_from_date(day)
            as_of_date = day.isoformat()

            for game in games:
                game_id = game.get("gameId")
                if not game_id:
                    continue
                rows = _fetch_boxscore_players(game_id)
                if rows:
                    time.sleep(API_DELAY)

                for row in rows:
                    mins_played = _minutes_played(row.get("MIN"))
                    if mins_played <= 0:
                        continue

                    player_id = int(row.get("PLAYER_ID", 0) or 0)
                    team_id = int(row.get("TEAM_ID", 0) or 0)
                    if player_id <= 0 or team_id <= 0:
                        continue

                    if team_id == game["homeTeamId"]:
                        is_home = True
                        opponent_abbr = game["awayAbbr"]
                    elif team_id == game["awayTeamId"]:
                        is_home = False
                        opponent_abbr = game["homeAbbr"]
                    else:
                        continue

                    is_b2b = team_id in b2b_team_ids
                    actual = _actual_stats(row)

                    for model_name in models:
                        acc = accumulators[model_name]
                        cache_key = (
                            model_name,
                            day.isoformat(),
                            player_id,
                            opponent_abbr,
                            int(is_home),
                            int(is_b2b),
                        )
                        if cache_key not in projection_cache:
                            fn = _model_callable(model_name)
                            projection_cache[cache_key] = fn(
                                player_id=player_id,
                                opponent_abbr=opponent_abbr,
                                is_home=is_home,
                                is_b2b=is_b2b,
                                season=season,
                                as_of_date=as_of_date,
                            )
                            acc["projectionCalls"] += 1

                        proj_data = projection_cache[cache_key]
                        if not proj_data.get("success"):
                            acc["projectionErrors"] += 1
                            continue

                        projections = proj_data.get("projections", {}) or {}
                        for stat in TRACKED_STATS:
                            proj_stat = projections.get(stat)
                            if not proj_stat:
                                continue
                            actual_val = actual.get(stat)
                            if actual_val is None:
                                continue

                            projected = float(proj_stat.get("projection") or 0.0)
                            line = _synthetic_line(proj_stat)
                            stdev_val = proj_stat.get("projStdev") or proj_stat.get("stdev") or 0
                            ev = compute_ev(
                                projected,
                                line,
                                DEFAULT_SYNTHETIC_OVER_ODDS,
                                DEFAULT_SYNTHETIC_UNDER_ODDS,
                                stdev=stdev_val,
                            )
                            if not ev:
                                continue

                            prob_over = float(ev.get("probOver", 0.5) or 0.5)
                            actual_over = 1.0 if actual_val > line else 0.0
                            err = abs(projected - actual_val)
                            brier = (prob_over - actual_over) ** 2

                            acc["sampleCount"] += 1
                            acc["samplesByStat"][stat] += 1
                            acc["maeSumByStat"][stat] += err
                            acc["brierSumByStat"][stat] += brier

                            bin_idx = max(0, min(9, int(prob_over * 10)))
                            cbin = acc["calibrationByStat"][stat][bin_idx]
                            cbin["count"] += 1
                            cbin["predOverSum"] += prob_over
                            cbin["actualOverHits"] += actual_over

                            over_side = ev.get("over") or {}
                            under_side = ev.get("under") or {}
                            over_ev = float(over_side.get("evPercent") or -1e9)
                            under_ev = float(under_side.get("evPercent") or -1e9)
                            if over_ev >= under_ev:
                                chosen_side = "over"
                                chosen = over_side
                                chosen_odds = DEFAULT_SYNTHETIC_OVER_ODDS
                            else:
                                chosen_side = "under"
                                chosen = under_side
                                chosen_odds = DEFAULT_SYNTHETIC_UNDER_ODDS

                            edge = abs(float(chosen.get("edge") or 0.0))
                            meets_threshold = bool(chosen.get("meetsThreshold")) or (
                                edge >= float(PROJECTION_CONFIG.get("min_edge_threshold", 0.03))
                            )
                            if float(chosen.get("evPercent") or 0.0) > 0.0 and meets_threshold:
                                outcome = _grade_side(actual_val, line, chosen_side)
                                pnl = _pnl_for_american(outcome, chosen_odds)
                                roi = acc["roiSimulation"]
                                roi["betsPlaced"] += 1
                                roi["pnlUnits"] += pnl
                                if outcome == "win":
                                    roi["wins"] += 1
                                elif outcome == "loss":
                                    roi["losses"] += 1
                                else:
                                    roi["pushes"] += 1

        model_reports = {m: _finalize_accumulator(accumulators[m]) for m in models}
        response = {
            "success": True,
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
            "days": (end - start).days + 1,
            "modelsEvaluated": models,
            "stats": TRACKED_STATS,
            "minEdgeThreshold": PROJECTION_CONFIG.get("min_edge_threshold", 0.03),
            "reports": model_reports,
        }

        if "full" in model_reports and "simple" in model_reports:
            comparison = {
                "maeDeltaSimpleMinusFull": {},
                "brierDeltaSimpleMinusFull": {},
                "roiDeltaSimpleMinusFullPctPerBet": None,
            }
            for stat in TRACKED_STATS:
                full_mae = model_reports["full"]["maeByStat"].get(stat)
                simple_mae = model_reports["simple"]["maeByStat"].get(stat)
                full_brier = model_reports["full"]["brierByStat"].get(stat)
                simple_brier = model_reports["simple"]["brierByStat"].get(stat)
                comparison["maeDeltaSimpleMinusFull"][stat] = (
                    safe_round(simple_mae - full_mae, 4)
                    if full_mae is not None and simple_mae is not None
                    else None
                )
                comparison["brierDeltaSimpleMinusFull"][stat] = (
                    safe_round(simple_brier - full_brier, 4)
                    if full_brier is not None and simple_brier is not None
                    else None
                )
            full_roi = model_reports["full"]["roiSimulation"].get("roiPctPerBet")
            simple_roi = model_reports["simple"]["roiSimulation"].get("roiPctPerBet")
            if full_roi is not None and simple_roi is not None:
                comparison["roiDeltaSimpleMinusFullPctPerBet"] = safe_round(simple_roi - full_roi, 4)
            response["comparison"] = comparison

        return response
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
        }
