#!/usr/bin/env python3
"""Historical backtesting utilities for projection and EV calibration."""

import json
import math
import os
import time
from datetime import datetime, timedelta

from nba_api.stats.endpoints import boxscoretraditionalv3, scoreboardv3
from nba_api.stats.static import players as nba_players_static

from .nba_data_collection import (
    API_DELAY,
    BETTING_POLICY,
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

# Version summary for comparing backtest runs across model changes.
# Update this when adding no-vig, regression-to-mean, etc.
MODEL_VERSION_SUMMARY = {
    "version": "v1",
    "projection": {
        "full": "defense (position-weighted), matchup history, position-vs-team, home/away, rest, pace factor",
        "simple": "defense only; no matchup or position-vs-team",
        "perMinRate": "60% weighted recent + 40% season rate",
        "statMultiplier": "defense * matchup, capped by PROJECTION_CONFIG.combined",
        "minutes": "base = weighted recent; adj = home/away, rest, trend; minutesMultiplier from nba_minutes_model (streak, volatility, B2B)",
    },
    "ev": {
        "distribution": "Poisson for stl,blk,fg3m,tov; Normal for pts,reb,ast,pra",
        "stdev": "rolling stdev or 20% of projection",
        "calibration": "per-stat temperature scaling from models/prob_calibration.json",
        "edge": "model probOver vs no-vig implied (over_implied / (over+under))",
        "noVigImplied": True,
    },
    "bettingPolicy": "statWhitelist, blockedProbBins, minEdgeThreshold from nba_data_collection",
}

# ---------------------------------------------------------------------------
# Date-aware policy versioning
# ---------------------------------------------------------------------------
_POLICY_HISTORY = None


def _get_policy_for_date(date_str):
    """Return the BETTING_POLICY that was active on *date_str* (YYYY-MM-DD).

    Loads ``models/policy_history.json`` (cached after first call).
    Falls back to the current ``BETTING_POLICY`` if the file is missing,
    invalid, or *date_str* is before every recorded entry.
    """
    global _POLICY_HISTORY
    if _POLICY_HISTORY is None:
        policy_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "models",
            "policy_history.json",
        )
        try:
            with open(policy_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, list) or len(raw) == 0:
                _POLICY_HISTORY = []
            else:
                _POLICY_HISTORY = sorted(raw, key=lambda e: e["effective_from"])
        except (OSError, json.JSONDecodeError, KeyError):
            _POLICY_HISTORY = []

    # Find the latest entry whose effective_from <= date_str
    matched = None
    for entry in _POLICY_HISTORY:
        if entry["effective_from"] <= date_str:
            matched = entry
        else:
            break

    if matched is None:
        # Before all recorded entries — fall back to current BETTING_POLICY
        return {
            "stat_whitelist": set(BETTING_POLICY.get("stat_whitelist", set())),
            "blocked_bins": set(BETTING_POLICY.get("blocked_prob_bins", set())),
            "min_edge": BETTING_POLICY.get("min_edge_threshold", 0.05),
        }

    return {
        "stat_whitelist": set(matched["stat_whitelist"]),
        "blocked_bins": set(int(b) for b in matched["blocked_bins"]),
        "min_edge": float(matched["min_edge"]),
    }


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
        "realLineSamples": 0,
        "missingLineSamples": 0,
        "roiReal":  {"betsPlaced": 0, "wins": 0, "losses": 0, "pushes": 0, "pnlUnits": 0.0},
        "roiSynth": {"betsPlaced": 0, "wins": 0, "losses": 0, "pushes": 0, "pnlUnits": 0.0},
        "realLineStatRoi": {s: {"betsPlaced": 0, "wins": 0, "losses": 0, "pushes": 0, "pnlUnits": 0.0}
                            for s in TRACKED_STATS},
        "realLineCalibBins": {i: {"count": 0, "wins": 0, "pnlUnits": 0.0} for i in range(10)},
        # Bet-level records (populated only when emit_bets=True)
        "_bet_records": [],
        # CLV tracking: opening line vs closing line per bet
        "clvBetsTracked":    0,
        "clvPositiveCount":  0,
        "clvLineSumPositive": 0.0,
        "clvLineSumNegative": 0.0,
        "roiClvPositive": {"betsPlaced": 0, "wins": 0, "losses": 0, "pushes": 0, "pnlUnits": 0.0},
        "roiClvNegative": {"betsPlaced": 0, "wins": 0, "losses": 0, "pushes": 0, "pnlUnits": 0.0},
    }


def _finalize_roi_seg(seg):
    b = seg["betsPlaced"]
    return {
        "betsPlaced":   b,
        "wins":         seg["wins"],
        "losses":       seg["losses"],
        "pushes":       seg["pushes"],
        "pnlUnits":     safe_round(seg["pnlUnits"], 4),
        "hitRatePct":   safe_round(seg["wins"] / b * 100.0, 3) if b else None,
        "roiPctPerBet": safe_round(seg["pnlUnits"] / b * 100.0, 3) if b else None,
    }


def _finalize_clv(acc):
    """Produce the clv summary dict from raw accumulator CLV fields."""
    n   = acc["clvBetsTracked"]
    pos = acc["clvPositiveCount"]
    neg = n - pos
    return {
        "betsTracked":        n,
        "positiveCount":      pos,
        "positiveClvPct":     safe_round(pos / n * 100.0, 2) if n else None,
        "avgClvLinePositive": safe_round(acc["clvLineSumPositive"] / pos, 3) if pos else None,
        "avgClvLineNegative": safe_round(acc["clvLineSumNegative"] / neg, 3) if neg else None,
        "roiClvPositive":     _finalize_roi_seg(acc["roiClvPositive"]),
        "roiClvNegative":     _finalize_roi_seg(acc["roiClvNegative"]),
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
        "realLineSamples": acc["realLineSamples"],
        "missingLineSamples": acc["missingLineSamples"],
        "roiReal":           _finalize_roi_seg(acc["roiReal"]),
        "roiSynth":          _finalize_roi_seg(acc["roiSynth"]),
        "realLineStatRoi":   {s: _finalize_roi_seg(acc["realLineStatRoi"][s])
                              for s in TRACKED_STATS},
        "clv": _finalize_clv(acc),
        "realLineCalibBins": [
            {
                "bin":          f"{i*10}-{(i+1)*10}%",
                "betsPlaced":   acc["realLineCalibBins"][i]["count"],
                "wins":         acc["realLineCalibBins"][i]["wins"],
                "pnlUnits":     safe_round(acc["realLineCalibBins"][i]["pnlUnits"], 4),
                "hitRatePct":   safe_round(
                    acc["realLineCalibBins"][i]["wins"] /
                    acc["realLineCalibBins"][i]["count"] * 100.0, 2)
                    if acc["realLineCalibBins"][i]["count"] > 0 else None,
                "roiPctPerBet": safe_round(
                    acc["realLineCalibBins"][i]["pnlUnits"] /
                    acc["realLineCalibBins"][i]["count"] * 100.0, 3)
                    if acc["realLineCalibBins"][i]["count"] > 0 else None,
            }
            for i in range(10)
        ],
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
    odds_source=None,
    odds_db=None,
    local_index=None,
    odds_only=False,
    compute_clv=False,
    walk_forward=False,
    emit_bets=False,
):
    """
    Backtest projection + EV quality for one day or a date range.

    save_results: if True, writes JSON to data/backtest_results/
    fast: if True, uses a reduced API delay (~2x speed, slightly higher ban risk)
    data_source: "nba" (default), "bref", or "local"
    bref_dir: optional override for BRef curated file directory
    odds_source: None (synthetic lines) or "local_history" (use OddsStore closing lines)
    odds_db: optional path override for the OddsStore SQLite database
    local_index: optional path override for LocalNBAStats index pickle
    odds_only: if True, skip any player-stat pair without a real closing line
               (no synthetic ±0.5 fallback). Requires odds_source="local_history".
               missingLineSamples will be 0 in the output.
    walk_forward: if True, use date-specific calibration (models/walk_forward/)
                  and date-specific policy (models/policy_history.json) for each
                  backtest day. Eliminates calibration lookahead and policy snooping.
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
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "data", "backtest_results"
    )

    model_key = str(model or "both").lower().strip()
    if model_key not in {"full", "simple", "both"}:
        return {"success": False, "error": "model must be one of: full, simple, both"}

    source_key = str(data_source or "nba").lower().strip()
    if source_key not in {"nba", "bref", "local"}:
        return {"success": False, "error": "data_source must be one of: nba, bref, local"}

    odds_key = str(odds_source or "").lower().strip()
    if odds_key and odds_key not in {"local_history"}:
        return {"success": False, "error": "odds_source must be 'local_history' or None"}

    odds_store = None
    _odds_stat_to_market = {}
    if odds_key == "local_history":
        try:
            from .nba_odds_store import OddsStore as _OddsStore
            from .nba_odds_store import STAT_TO_MARKET as _STM
            odds_store = _OddsStore(db_path=odds_db)
            _odds_stat_to_market = _STM
        except Exception as e:
            return {"success": False, "error": f"Failed to init OddsStore: {e}"}

    # Cache opening lines to avoid redundant DB queries (keyed by event_id+market+player)
    _open_line_cache = {}

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
            local_provider = LocalNBAStats(index_path=local_index)
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
    event_id_cache = {}   # (date_str, homeAbbr, awayAbbr) -> odds event_id or None
    player_name_cache = {}  # player_id -> full_name string

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
            _day_policy = _get_policy_for_date(as_of_date) if walk_forward else None

            day_proj_calls = 0
            day_samples_start = sum(accumulators[m]["sampleCount"] for m in models)

            for game in games:
                game_id = game.get("gameId")
                if not game_id:
                    continue

                # Resolve Odds API event_id for this game (used for real-line lookup).
                odds_event_id = None
                if odds_store is not None:
                    _ev_key = (day.isoformat(), game["homeAbbr"], game["awayAbbr"])
                    if _ev_key not in event_id_cache:
                        event_id_cache[_ev_key] = odds_store.find_event_for_game(
                            game["homeAbbr"], game["awayAbbr"], day.isoformat()
                        )
                    odds_event_id = event_id_cache.get(_ev_key)

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

                    # Cache player full name for odds line lookup.
                    if player_id not in player_name_cache:
                        _pobj = nba_players_static.find_player_by_id(player_id)
                        player_name_cache[player_id] = (
                            _pobj.get("full_name", "") if _pobj else ""
                        )
                    _player_full_name = player_name_cache[player_id]

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
                            stdev_val = proj_stat.get("projStdev") or proj_stat.get("stdev") or 0

                            # Determine line source: real closing line or synthetic.
                            over_odds  = DEFAULT_SYNTHETIC_OVER_ODDS
                            under_odds = DEFAULT_SYNTHETIC_UNDER_ODDS
                            line       = _synthetic_line(proj_stat)
                            _used_real_line = False
                            if (
                                odds_store is not None
                                and odds_event_id
                                and _player_full_name
                            ):
                                _market = _odds_stat_to_market.get(stat)
                                if _market:
                                    _cl = odds_store.get_closing_line(
                                        odds_event_id, _market, _player_full_name
                                    )
                                    if _cl and _cl.get("close_line") is not None:
                                        line = _cl["close_line"]
                                        if _cl.get("close_over_odds"):
                                            over_odds = _cl["close_over_odds"]
                                        if _cl.get("close_under_odds"):
                                            under_odds = _cl["close_under_odds"]
                                        acc["realLineSamples"] += 1
                                        _used_real_line = True
                                    else:
                                        if not odds_only:
                                            acc["missingLineSamples"] += 1

                            # --real-only: skip samples without a real closing line
                            if odds_only and not _used_real_line:
                                continue

                            _as_of = day.isoformat() if walk_forward else None
                            ev = compute_ev(
                                projected,
                                line,
                                over_odds,
                                under_odds,
                                stdev=stdev_val,
                                stat=stat,
                                as_of_date=_as_of,
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
                                chosen_odds = over_odds
                            else:
                                chosen_side = "under"
                                chosen = under_side
                                chosen_odds = under_odds

                            edge = abs(float(chosen.get("edge") or 0.0))
                            if walk_forward:
                                _stat_ok = stat in _day_policy["stat_whitelist"]
                                _bin_ok = bin_idx not in _day_policy["blocked_bins"]
                                meets_threshold = edge >= _day_policy["min_edge"]
                            else:
                                _bp = BETTING_POLICY
                                _stat_ok = stat in _bp.get("stat_whitelist", TRACKED_STATS)
                                _bin_ok = bin_idx not in _bp.get("blocked_prob_bins", set())
                                meets_threshold = bool(chosen.get("meetsThreshold")) or (
                                    edge >= float(PROJECTION_CONFIG.get("min_edge_threshold", 0.05))
                                )
                            if (
                                float(chosen.get("evPercent") or 0.0) > 0.0
                                and meets_threshold
                                and _stat_ok
                                and _bin_ok
                            ):
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

                                # Segmented ROI tracking
                                seg = acc["roiReal"] if _used_real_line else acc["roiSynth"]
                                seg["betsPlaced"] += 1
                                seg["pnlUnits"]   += pnl
                                if outcome == "win":
                                    seg["wins"] += 1
                                elif outcome == "loss":
                                    seg["losses"] += 1
                                else:
                                    seg["pushes"] += 1
                                if _used_real_line:
                                    sr = acc["realLineStatRoi"][stat]
                                    sr["betsPlaced"] += 1
                                    sr["pnlUnits"]   += pnl
                                    if outcome == "win":
                                        sr["wins"] += 1
                                    elif outcome == "loss":
                                        sr["losses"] += 1
                                    else:
                                        sr["pushes"] += 1
                                    cb = acc["realLineCalibBins"][bin_idx]
                                    cb["count"]    += 1
                                    cb["pnlUnits"] += pnl
                                    if outcome == "win":
                                        cb["wins"] += 1

                                    # CLV: compare opening line to closing line
                                    if compute_clv and odds_store is not None and odds_event_id:
                                        _clv_key = (odds_event_id, _market, _player_full_name)
                                        if _clv_key not in _open_line_cache:
                                            _open_line_cache[_clv_key] = odds_store.get_opening_line(
                                                odds_event_id, _market, _player_full_name
                                            )
                                        _open = _open_line_cache[_clv_key]
                                        if _open and _open.get("open_line") is not None:
                                            # clv_line > 0 means the opening line was better for our side
                                            # UNDER: good when open_line > close_line (higher threshold at open)
                                            # OVER:  good when open_line < close_line (lower threshold at open)
                                            _clv_line = (
                                                (_open["open_line"] - line)
                                                * (1 if chosen_side == "under" else -1)
                                            )
                                            acc["clvBetsTracked"] += 1
                                            if _clv_line > 0:
                                                acc["clvPositiveCount"]   += 1
                                                acc["clvLineSumPositive"] += _clv_line
                                                _clv_seg = acc["roiClvPositive"]
                                            else:
                                                acc["clvLineSumNegative"] += _clv_line
                                                _clv_seg = acc["roiClvNegative"]
                                            _clv_seg["betsPlaced"] += 1
                                            _clv_seg["pnlUnits"]   += pnl
                                            if outcome == "win":
                                                _clv_seg["wins"] += 1
                                            elif outcome == "loss":
                                                _clv_seg["losses"] += 1
                                            else:
                                                _clv_seg["pushes"] += 1

                                # Emit bet-level record for downstream analysis
                                if emit_bets:
                                    _ps = proj_stat or {}
                                    acc["_bet_records"].append({
                                        "date": day.isoformat(),
                                        "player_id": player_id,
                                        "player_name": _player_full_name,
                                        "stat": stat,
                                        "line": float(line),
                                        "projection": float(projected),
                                        "prob_over": float(prob_over),
                                        "bin": bin_idx,
                                        "side": chosen_side,
                                        "edge": float(edge),
                                        "odds": int(chosen_odds),
                                        "actual": float(actual_val),
                                        "outcome": outcome,
                                        "pnl": float(pnl),
                                        "used_real_line": _used_real_line,
                                        "n_games": int(_ps.get("nGames") or 0),
                                        "shrink_weight": float(_ps.get("shrinkWeight") or 0.0),
                                    })

            # End-of-day summary + checkpoint.
            day_samples = sum(accumulators[m]["sampleCount"] for m in models) - day_samples_start
            print(
                f"  -> {day_proj_calls} projections | {day_samples} samples",
                file=_sys.stderr,
                flush=True,
            )
            if save_results and day_proj_calls > 0:
                _os.makedirs(_results_dir, exist_ok=True)
                _ckpt_copies = {}
                for m in models:
                    _c = _copy.deepcopy(accumulators[m])
                    _c.pop("_bet_records", None)
                    _ckpt_copies[m] = _c
                _ckpt_reports = {
                    m: _finalize_accumulator(_ckpt_copies[m]) for m in models
                }
                _ckpt = {
                    "success": True,
                    "checkpoint": True,
                    "dataSource": source_key,
                    "dateFrom": start.isoformat(),
                    "dateTo": day.isoformat(),
                    "modelsEvaluated": models,
                    "stats": TRACKED_STATS,
                    "minEdgeThreshold": PROJECTION_CONFIG.get("min_edge_threshold", 0.05),
                    "modelVersion": MODEL_VERSION_SUMMARY,
                    "reports": _ckpt_reports,
                }
                _ckpt_fname = (
                    f"ckpt_{start.isoformat()}_to_{end.isoformat()}_"
                    f"{model_key}_{source_key}_{day.isoformat()}.json"
                )
                with open(_os.path.join(_results_dir, _ckpt_fname), "w", encoding="utf-8") as _fh:
                    _json.dump(_ckpt, _fh, indent=2)
                print(f"  -> checkpoint saved: {_ckpt_fname}", file=_sys.stderr, flush=True)

        # Pop bet records before finalization (not needed by _finalize_accumulator)
        _bet_records_by_model = {}
        if emit_bets:
            for m in models:
                _bet_records_by_model[m] = accumulators[m].pop("_bet_records", [])
        else:
            for m in models:
                accumulators[m].pop("_bet_records", None)

        model_reports = {m: _finalize_accumulator(accumulators[m]) for m in models}
        response = {
            "success": True,
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
            "days": (end - start).days + 1,
            "dataSource": source_key,
            "modelsEvaluated": models,
            "stats": TRACKED_STATS,
            "minEdgeThreshold": PROJECTION_CONFIG.get("min_edge_threshold", 0.05),
            "bettingPolicy": (
                {"mode": "walk_forward", "source": "models/policy_history.json"}
                if walk_forward
                else {
                    "statWhitelist": sorted(BETTING_POLICY.get("stat_whitelist", set())),
                    "blockedProbBins": sorted(BETTING_POLICY.get("blocked_prob_bins", set())),
                }
            ),
            "modelVersion": MODEL_VERSION_SUMMARY,
            "reports": model_reports,
        }
        if source_key == "bref" and bref_summary:
            response["brefCoverage"] = bref_summary
        if source_key == "local" and local_provider:
            response["localIndexPath"] = getattr(local_provider, "index_path", None)

        response["fast"] = fast
        response["walkForward"] = walk_forward
        if odds_key:
            response["oddsSource"] = odds_key
        if odds_only:
            response["oddsOnly"] = True

        # Attach bet-level records when emit_bets is enabled
        if emit_bets and _bet_records_by_model:
            if len(models) == 1:
                response["bets"] = _bet_records_by_model[models[0]]
            else:
                response["bets"] = _bet_records_by_model

        if save_results:
            results_dir = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "data", "backtest_results"
            )
            _os.makedirs(results_dir, exist_ok=True)
            model_tag = model_key.replace(",", "-")
            realonly_tag = "_realonly" if odds_only else ""
            wf_tag = "_wf" if walk_forward else ""
            fname = f"{start.isoformat()}_to_{end.isoformat()}_{model_tag}_{source_key}{realonly_tag}{wf_tag}.json"
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

        if odds_store is not None:
            odds_store.close()


def run_minutes_eval(date_from, date_to=None, data_source="nba", bref_dir=None, local_index=None):
    """
    Evaluate minutes projection accuracy over a date range.

    Runs compute_projection() for every player-game in the date window and
    compares minutesProjection.projectedMinutes against actual minutes played.

    Returns MAE, bias, sample count, and calibration buckets
    (from nba_minutes_model.minutes_calibration_bins).

    local_index: optional path override for LocalNBAStats index pickle.
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
            local_provider = LocalNBAStats(index_path=local_index)
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
        "localIndexPath":   getattr(local_provider, "index_path", None) if local_provider else None,
        "projectionErrors": errors,
        **cal,
    }
