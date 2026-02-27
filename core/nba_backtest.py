#!/usr/bin/env python3
"""Historical backtesting utilities for projection and EV calibration."""

import math
import time
from datetime import datetime, timedelta

from nba_api.stats.endpoints import boxscoretraditionalv3, scoreboardv3
from nba_api.stats.static import players as nba_players_static

from .nba_data_collection import (
    API_DELAY,
    HEADERS,
    PROJECTION_CONFIG,
    retry_api_call,
    safe_round,
)
from .nba_bref_data import load_bref_store
from .nba_data_prep import compute_projection, compute_projection_simple
from .nba_model_training import compute_ev

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


def _fetch_games_for_date(date_obj, data_source="nba", bref_store=None, local_provider=None):
    if data_source == "bref":
        if bref_store is None:
            raise RuntimeError("BRef store not initialized")
        return bref_store.get_games_for_date(date_obj)

    if data_source == "local":
        if local_provider is None:
            raise RuntimeError("Local provider not initialized")
        return local_provider.get_games_for_date(date_obj.strftime("%Y-%m-%d"))

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


def _teams_played_on_date(date_obj, cache, data_source="nba", bref_store=None, local_provider=None):
    key = f"{data_source}:{date_obj.isoformat()}"
    if key in cache:
        return cache[key]
    try:
        games = _fetch_games_for_date(date_obj, data_source=data_source, bref_store=bref_store, local_provider=local_provider)
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


def _fetch_boxscore_players(game_id, data_source="nba", bref_store=None, local_provider=None):
    if data_source == "bref":
        if bref_store is None:
            raise RuntimeError("BRef store not initialized")
        return bref_store.get_boxscore_players(game_id)

    if data_source == "local":
        if local_provider is None:
            raise RuntimeError("Local provider not initialized")
        return local_provider.get_boxscore_players(game_id)

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
        "minutesSampleCount": 0,
        "minutesMaeSum": 0.0,
        "minutesBiasSum": 0.0,
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

    mins_n = acc["minutesSampleCount"]
    minutes_mae  = safe_round(acc["minutesMaeSum"]  / mins_n, 4) if mins_n > 0 else None
    minutes_bias = safe_round(acc["minutesBiasSum"] / mins_n, 4) if mins_n > 0 else None

    return {
        "sampleCount": acc["sampleCount"],
        "projectionCalls": acc["projectionCalls"],
        "projectionErrors": acc["projectionErrors"],
        "maeByStat": mae,
        "brierByStat": brier,
        "calibrationByStat": calibration,
        "roiSimulation": roi,
        "minutesMae": minutes_mae,
        "minutesBias": minutes_bias,
        "minutesSampleCount": mins_n,
    }


def _model_callable(model_name):
    if model_name == "simple":
        return compute_projection_simple
    return compute_projection


def run_backtest(
    date_from,
    date_to=None,
    model="both",
    save_results=False,
    fast=False,
    data_source="nba",
    bref_dir=None,
):
    """
    Backtest projection + EV quality for one day or a date range.

    save_results: if True, writes JSON to data/backtest_results/
    fast: if True, uses a reduced API delay (~2x speed, slightly higher ban risk)
    data_source: "nba" (default), "bref", or "local"
    bref_dir: optional override for BRef curated file directory
    """
    import copy as _copy
    import json as _json
    import os as _os
    import sys as _sys

    start = _parse_date(date_from)
    end = _parse_date(date_to) if date_to else start
    if not start:
        return {"success": False, "error": "Invalid date_from. Use YYYY-MM-DD."}
    if not end:
        return {"success": False, "error": "Invalid date_to. Use YYYY-MM-DD."}
    if end < start:
        return {"success": False, "error": "date_to must be >= date_from."}

    _delay = 0.35 if fast else API_DELAY
    _results_dir = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), "data", "backtest_results"
    )

    model_key = str(model or "both").lower().strip()
    if model_key not in {"full", "simple", "both"}:
        return {"success": False, "error": "model must be one of: full, simple, both"}

    source_key = str(data_source or "nba").lower().strip()
    if source_key not in {"nba", "bref", "local"}:
        return {"success": False, "error": "data_source must be one of: nba, bref, local"}

    bref_store = None
    bref_summary = None
    if source_key == "bref":
        try:
            bref_store = load_bref_store(base_dir=bref_dir)
            bref_summary = bref_store.coverage_summary()
        except Exception as e:
            return {"success": False, "error": f"Failed to initialize BRef data source: {e}"}

    local_provider = None
    if source_key == "local":
        try:
            from .nba_local_stats import LocalNBAStats
            local_provider = LocalNBAStats()
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"Failed to initialize local data source: {e}"}

        # Coverage check: avoid partial local coverage.
        if local_provider.max_date and end.isoformat() > local_provider.max_date:
            import sys as _sys_tmp
            print(
                f"[local] WARNING: requested end date {end} exceeds local index max_date "
                f"{local_provider.max_date}. Falling back to 'nba' data source for this run.",
                file=_sys_tmp.stderr, flush=True,
            )
            source_key = "nba"
            local_provider = None

    models = ["full", "simple"] if model_key == "both" else [model_key]
    accumulators = {m: _new_accumulator() for m in models}
    projection_cache = {}
    schedule_cache = {}

    print(
        f"[backtest] {start} -> {end}  model={model_key}  delay={_delay:.2f}s  "
        f"save={save_results}  source={source_key}",
        file=_sys.stderr,
        flush=True,
    )
    if source_key == "bref" and bref_summary:
        print(
            f"[bref] days={bref_summary['days']} games={bref_summary['games']} "
            f"players={bref_summary['playerRows']}",
            file=_sys.stderr,
            flush=True,
        )

    if source_key == "local" and local_provider:
        _first = local_provider.seasons_covered[0] if local_provider.seasons_covered else "?"
        _last = local_provider.seasons_covered[-1] if local_provider.seasons_covered else "?"
        print(
            f"[local] index loaded  seasons={_first}-{_last}"
            f"  date_range={local_provider.min_date or '?'}->{local_provider.max_date or '?'}"
            f"  schema={getattr(local_provider, 'schema', 'unknown')}",
            file=_sys.stderr,
            flush=True,
        )

    # For "local" source: monkey-patch both nba_data_collection and
    # nba_prep_projection module-level bindings so compute_projection
    # resolves to local providers without API calls.
    from . import nba_data_collection as _dc
    from . import nba_prep_projection as _pp

    _orig_gamelog = _dc.get_player_game_log
    _orig_splits = _dc.get_player_splits
    _orig_defense = _dc.get_team_defensive_ratings
    _orig_pos = _dc.get_player_position
    _orig_pvt = _dc.get_position_vs_team

    _orig_pp_gamelog = _pp.get_player_game_log
    _orig_pp_splits = _pp.get_player_splits
    _orig_pp_defense = _pp.get_team_defensive_ratings
    _orig_pp_pos = _pp.get_player_position
    _orig_pp_pvt = _pp.get_position_vs_team
    _orig_pp_api_delay = _pp.API_DELAY

    if source_key == "local" and local_provider:
        _dc.get_player_game_log = local_provider.get_player_game_log
        _dc.get_player_splits = local_provider.get_player_splits
        _dc.get_team_defensive_ratings = local_provider.get_team_defensive_ratings
        _dc.get_player_position = local_provider.get_player_position
        _dc.get_position_vs_team = local_provider.get_position_vs_team

        _pp.get_player_game_log = local_provider.get_player_game_log
        _pp.get_player_splits = local_provider.get_player_splits
        _pp.get_team_defensive_ratings = local_provider.get_team_defensive_ratings
        _pp.get_player_position = local_provider.get_player_position
        _pp.get_position_vs_team = local_provider.get_position_vs_team
        _pp.API_DELAY = 0.0

    try:
        for day in _iter_dates(start, end):
            games = _fetch_games_for_date(day, data_source=source_key, bref_store=bref_store, local_provider=local_provider)
            if not games:
                print(f"[{day}] no games - skipping", file=_sys.stderr, flush=True)
                continue
            print(f"[{day}] {len(games)} games", file=_sys.stderr, flush=True)
            b2b_team_ids = _teams_played_on_date(
                day - timedelta(days=1),
                schedule_cache,
                data_source=source_key,
                bref_store=bref_store,
                local_provider=local_provider,
            )
            season = _season_from_date(day)
            as_of_date = day.isoformat()

            day_proj_calls = 0
            day_samples_start = sum(accumulators[m]["sampleCount"] for m in models)

            for game in games:
                game_id = game.get("gameId")
                if not game_id:
                    continue
                rows = _fetch_boxscore_players(
                    game_id,
                    data_source=source_key,
                    bref_store=bref_store,
                    local_provider=local_provider,
                )
                if rows and source_key == "nba":
                    time.sleep(_delay)

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
                            day_proj_calls += 1
                            _pobj = nba_players_static.find_player_by_id(player_id)
                            _pname = _pobj.get("full_name", str(player_id)) if _pobj else str(player_id)
                            _side = "vs" if is_home else "@"
                            print(
                                f"  [{day_proj_calls:>3}] {_pname} {_side} {opponent_abbr}",
                                file=_sys.stderr,
                                flush=True,
                            )

                        proj_data = projection_cache[cache_key]
                        if not proj_data.get("success"):
                            acc["projectionErrors"] += 1
                            continue

                        projections = proj_data.get("projections", {}) or {}

                        # Track minutes accuracy (once per player-game, before stat loop)
                        _mins_ctx = proj_data.get("minutesProjection", {}) or {}
                        _proj_mins = float(_mins_ctx.get("projectedMinutes") or 0.0)
                        if _proj_mins > 0 and mins_played > 0:
                            acc["minutesSampleCount"] += 1
                            acc["minutesMaeSum"]  += abs(_proj_mins - mins_played)
                            acc["minutesBiasSum"] += (_proj_mins - mins_played)

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

            # End-of-day summary + checkpoint.
            day_samples = sum(accumulators[m]["sampleCount"] for m in models) - day_samples_start
            print(
                f"  -> {day_proj_calls} projections | {day_samples} samples",
                file=_sys.stderr,
                flush=True,
            )
            if save_results and day_proj_calls > 0:
                _os.makedirs(_results_dir, exist_ok=True)
                _ckpt_reports = {
                    m: _finalize_accumulator(_copy.deepcopy(accumulators[m])) for m in models
                }
                _ckpt = {
                    "success": True,
                    "checkpoint": True,
                    "dataSource": source_key,
                    "dateFrom": start.isoformat(),
                    "dateTo": day.isoformat(),
                    "modelsEvaluated": models,
                    "stats": TRACKED_STATS,
                    "minEdgeThreshold": PROJECTION_CONFIG.get("min_edge_threshold", 0.03),
                    "reports": _ckpt_reports,
                }
                _ckpt_fname = (
                    f"ckpt_{start.isoformat()}_to_{end.isoformat()}_"
                    f"{model_key}_{source_key}_{day.isoformat()}.json"
                )
                with open(_os.path.join(_results_dir, _ckpt_fname), "w", encoding="utf-8") as _fh:
                    _json.dump(_ckpt, _fh, indent=2)
                print(f"  -> checkpoint saved: {_ckpt_fname}", file=_sys.stderr, flush=True)

        model_reports = {m: _finalize_accumulator(accumulators[m]) for m in models}
        response = {
            "success": True,
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
            "days": (end - start).days + 1,
            "dataSource": source_key,
            "modelsEvaluated": models,
            "stats": TRACKED_STATS,
            "minEdgeThreshold": PROJECTION_CONFIG.get("min_edge_threshold", 0.03),
            "reports": model_reports,
        }
        if source_key == "bref" and bref_summary:
            response["brefCoverage"] = bref_summary

        response["fast"] = fast

        if save_results:
            results_dir = _os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)), "data", "backtest_results"
            )
            _os.makedirs(results_dir, exist_ok=True)
            model_tag = model_key.replace(",", "-")
            fname = f"{start.isoformat()}_to_{end.isoformat()}_{model_tag}_{source_key}.json"
            fpath = _os.path.join(results_dir, fname)
            with open(fpath, "w", encoding="utf-8") as fh:
                _json.dump(response, fh, indent=2)
            response["savedTo"] = fpath

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
            "dataSource": source_key,
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
        }
    finally:
        # Always restore monkey-patched functions
        _dc.get_player_game_log = _orig_gamelog
        _dc.get_player_splits = _orig_splits
        _dc.get_team_defensive_ratings = _orig_defense
        _dc.get_player_position = _orig_pos
        _dc.get_position_vs_team = _orig_pvt

        _pp.get_player_game_log = _orig_pp_gamelog
        _pp.get_player_splits = _orig_pp_splits
        _pp.get_team_defensive_ratings = _orig_pp_defense
        _pp.get_player_position = _orig_pp_pos
        _pp.get_position_vs_team = _orig_pp_pvt
        _pp.API_DELAY = _orig_pp_api_delay


def run_minutes_eval(date_from, date_to=None, data_source="nba", bref_dir=None):
    """
    Evaluate minutes projection accuracy over a date range.

    Runs compute_projection() for every player-game in the date window and
    compares minutesProjection.projectedMinutes against actual minutes played.

    Returns MAE, bias, sample count, and calibration buckets
    (from nba_minutes_model.minutes_calibration_bins).
    """
    from .nba_minutes_model import minutes_calibration_bins

    import sys as _sys

    start = _parse_date(date_from)
    end   = _parse_date(date_to) if date_to else start
    if not start:
        return {"success": False, "error": "Invalid date_from. Use YYYY-MM-DD."}
    if not end:
        return {"success": False, "error": "Invalid date_to. Use YYYY-MM-DD."}
    if end < start:
        return {"success": False, "error": "date_to must be >= date_from."}

    source_key = str(data_source or "nba").lower().strip()
    if source_key not in {"nba", "bref", "local"}:
        return {"success": False, "error": "data_source must be one of: nba, bref, local"}

    bref_store = None
    if source_key == "bref":
        try:
            bref_store = load_bref_store(base_dir=bref_dir)
        except Exception as e:
            return {"success": False, "error": f"Failed to init BRef: {e}"}

    local_provider = None
    if source_key == "local":
        try:
            from .nba_local_stats import LocalNBAStats
            local_provider = LocalNBAStats()
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"Failed to init local: {e}"}

    from . import nba_data_collection as _dc
    from . import nba_prep_projection as _pp

    _orig_gamelog  = _dc.get_player_game_log
    _orig_splits   = _dc.get_player_splits
    _orig_defense  = _dc.get_team_defensive_ratings
    _orig_pos      = _dc.get_player_position
    _orig_pvt      = _dc.get_position_vs_team

    _orig_pp_gamelog = _pp.get_player_game_log
    _orig_pp_splits  = _pp.get_player_splits
    _orig_pp_defense = _pp.get_team_defensive_ratings
    _orig_pp_pos     = _pp.get_player_position
    _orig_pp_pvt     = _pp.get_position_vs_team
    _orig_pp_delay   = _pp.API_DELAY

    if source_key == "local" and local_provider:
        _dc.get_player_game_log        = local_provider.get_player_game_log
        _dc.get_player_splits          = local_provider.get_player_splits
        _dc.get_team_defensive_ratings = local_provider.get_team_defensive_ratings
        _dc.get_player_position        = local_provider.get_player_position
        _dc.get_position_vs_team       = local_provider.get_position_vs_team

        _pp.get_player_game_log        = local_provider.get_player_game_log
        _pp.get_player_splits          = local_provider.get_player_splits
        _pp.get_team_defensive_ratings = local_provider.get_team_defensive_ratings
        _pp.get_player_position        = local_provider.get_player_position
        _pp.get_position_vs_team       = local_provider.get_position_vs_team
        _pp.API_DELAY = 0.0

    paired_list = []
    projection_cache = {}
    schedule_cache = {}
    errors = 0

    try:
        for day in _iter_dates(start, end):
            games = _fetch_games_for_date(
                day, data_source=source_key, bref_store=bref_store, local_provider=local_provider
            )
            if not games:
                continue
            print(f"[minutes_eval] {day} {len(games)} games", file=_sys.stderr, flush=True)

            b2b_team_ids = _teams_played_on_date(
                day - timedelta(days=1), schedule_cache,
                data_source=source_key, bref_store=bref_store, local_provider=local_provider,
            )
            season     = _season_from_date(day)
            as_of_date = day.isoformat()

            for game in games:
                game_id = game.get("gameId")
                if not game_id:
                    continue
                rows = _fetch_boxscore_players(
                    game_id, data_source=source_key,
                    bref_store=bref_store, local_provider=local_provider,
                )
                for row in rows:
                    mins_played = _minutes_played(row.get("MIN"))
                    if mins_played <= 0:
                        continue
                    player_id = int(row.get("PLAYER_ID", 0) or 0)
                    team_id   = int(row.get("TEAM_ID", 0) or 0)
                    if player_id <= 0 or team_id <= 0:
                        continue

                    if team_id == game["homeTeamId"]:
                        is_home       = True
                        opponent_abbr = game["awayAbbr"]
                    elif team_id == game["awayTeamId"]:
                        is_home       = False
                        opponent_abbr = game["homeAbbr"]
                    else:
                        continue

                    is_b2b = team_id in b2b_team_ids
                    cache_key = (day.isoformat(), player_id, opponent_abbr, int(is_home), int(is_b2b))

                    if cache_key not in projection_cache:
                        try:
                            projection_cache[cache_key] = compute_projection(
                                player_id=player_id,
                                opponent_abbr=opponent_abbr,
                                is_home=is_home,
                                is_b2b=is_b2b,
                                season=season,
                                as_of_date=as_of_date,
                            )
                        except Exception:
                            projection_cache[cache_key] = {"success": False}
                            errors += 1

                    proj_data = projection_cache[cache_key]
                    if not proj_data.get("success"):
                        continue

                    mins_ctx  = proj_data.get("minutesProjection", {}) or {}
                    proj_mins = float(mins_ctx.get("projectedMinutes") or 0.0)
                    if proj_mins > 0:
                        paired_list.append((proj_mins, mins_played))

    finally:
        _dc.get_player_game_log        = _orig_gamelog
        _dc.get_player_splits          = _orig_splits
        _dc.get_team_defensive_ratings = _orig_defense
        _dc.get_player_position        = _orig_pos
        _dc.get_position_vs_team       = _orig_pvt

        _pp.get_player_game_log        = _orig_pp_gamelog
        _pp.get_player_splits          = _orig_pp_splits
        _pp.get_team_defensive_ratings = _orig_pp_defense
        _pp.get_player_position        = _orig_pp_pos
        _pp.get_position_vs_team       = _orig_pp_pvt
        _pp.API_DELAY                  = _orig_pp_delay

    cal = minutes_calibration_bins(paired_list)
    return {
        "success":          True,
        "dateFrom":         start.isoformat(),
        "dateTo":           end.isoformat(),
        "dataSource":       source_key,
        "projectionErrors": errors,
        **cal,
    }
