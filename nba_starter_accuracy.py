#!/usr/bin/env python3
"""Pregame EV accuracy evaluation for all starters on a slate."""

import traceback
from datetime import datetime, timedelta

import requests
from nba_api.stats.endpoints import scoreboardv3
from nba_api.stats.static import players as nba_players_static

from nba_data_collection import (
    API_DELAY,
    ODDS_PLAYER_PROP_MARKET_BY_STAT,
    PROJECTION_CONFIG,
    _extract_player_offer_side_and_name,
    _odds_api_get,
    _player_name_matches,
    _team_name_matches_abbr,
    cache_get,
    cache_set,
    retry_api_call,
    safe_div,
    safe_round,
)
from nba_data_prep import compute_projection
from nba_ev_engine import compute_ev

TRACKED_STATS = ["pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "pra"]
DEFAULT_BOOKMAKERS = "draftkings,fanduel"
DEFAULT_REGIONS = "us"
DEFAULT_SPORT = "basketball_nba"
DEFAULT_MODEL_VARIANT = "full"

_PLAYER_NAME_BY_ID = {
    int(p["id"]): str(p.get("full_name", ""))
    for p in nba_players_static.get_players()
    if p.get("id")
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


def _fetch_games_for_date(date_obj):
    date_str = date_obj.strftime("%Y-%m-%d")

    def fetch():
        return scoreboardv3.ScoreboardV3(
            game_date=date_str,
            league_id="00",
            timeout=30,
        ).get_dict()

    data = retry_api_call(fetch)
    out = []
    for g in (data.get("scoreboard", {}) or {}).get("games", []) or []:
        home = g.get("homeTeam", {}) or {}
        away = g.get("awayTeam", {}) or {}
        out.append(
            {
                "gameId": str(g.get("gameId", "") or ""),
                "gameStatus": int(g.get("gameStatus", 0) or 0),
                "gameTimeUTC": str(g.get("gameTimeUTC", "") or ""),
                "homeTeamId": int(home.get("teamId", 0) or 0),
                "awayTeamId": int(away.get("teamId", 0) or 0),
                "homeAbbr": str(home.get("teamTricode", "") or "").upper(),
                "awayAbbr": str(away.get("teamTricode", "") or "").upper(),
            }
        )
    return out


def _teams_played_on_date(date_obj):
    try:
        games = _fetch_games_for_date(date_obj)
    except Exception:
        return set()
    out = set()
    for g in games:
        out.add(int(g.get("homeTeamId", 0) or 0))
        out.add(int(g.get("awayTeamId", 0) or 0))
    return out


def _event_match_id(game, sport, regions, bookmakers, game_time_utc):
    params = {
        "regions": regions,
        "markets": "h2h",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if bookmakers:
        params["bookmakers"] = bookmakers

    snapshot_ts = None
    raw_ts = str(game_time_utc or "").strip()
    if raw_ts:
        try:
            game_dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            snapshot_ts = (game_dt - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
            params["date"] = snapshot_ts
        except Exception:
            snapshot_ts = None

    response = _odds_api_get(f"historical/sports/{sport}/odds", params=params, timeout=40)
    if not response.get("success"):
        return None, snapshot_ts, response.get("error"), response.get("details")

    raw_payload = response.get("data") or {}
    if isinstance(raw_payload, dict):
        events = raw_payload.get("data", []) or []
    elif isinstance(raw_payload, list):
        events = raw_payload
    else:
        events = []
    strict = []
    relaxed = []
    home_abbr = str(game.get("homeAbbr", "")).upper()
    away_abbr = str(game.get("awayAbbr", "")).upper()

    for ev in events:
        home_name = ev.get("home_team", "")
        away_name = ev.get("away_team", "")
        home_ok = _team_name_matches_abbr(home_name, home_abbr)
        away_ok = _team_name_matches_abbr(away_name, away_abbr)
        if home_ok and away_ok:
            strict.append(ev)
            continue

        has_home = home_ok or _team_name_matches_abbr(away_name, home_abbr)
        has_away = away_ok or _team_name_matches_abbr(home_name, away_abbr)
        if has_home and has_away:
            relaxed.append(ev)

    candidates = strict or relaxed
    if not candidates:
        return None, snapshot_ts, "No matching historical event found.", None

    def _sort_key(ev):
        ts = str(ev.get("commence_time", "") or "")
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return datetime.max

    selected = sorted(candidates, key=_sort_key)[0]
    return str(selected.get("id", "") or ""), snapshot_ts, None, None


def _fetch_event_prop_offers(event_id, snapshot_ts, sport, regions, bookmakers):
    markets_csv = ",".join(
        ODDS_PLAYER_PROP_MARKET_BY_STAT[s]
        for s in TRACKED_STATS
        if ODDS_PLAYER_PROP_MARKET_BY_STAT.get(s)
    )
    params = {
        "regions": regions,
        "markets": markets_csv,
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if snapshot_ts:
        params["date"] = snapshot_ts
    if bookmakers:
        params["bookmakers"] = bookmakers

    resp = _odds_api_get(
        f"historical/sports/{sport}/events/{event_id}/odds",
        params=params,
        timeout=40,
    )
    if not resp.get("success"):
        return {"success": False, "error": resp.get("error"), "details": resp.get("details")}

    raw_payload = resp.get("data") or {}
    if isinstance(raw_payload, dict):
        payload = raw_payload.get("data", {}) or {}
    else:
        payload = {}
    inv_market = {
        mk: stat
        for stat, mk in ODDS_PLAYER_PROP_MARKET_BY_STAT.items()
        if stat in TRACKED_STATS
    }
    offers_by_stat = {s: [] for s in TRACKED_STATS}

    for bm in payload.get("bookmakers", []) or []:
        book_name = bm.get("title") or bm.get("key") or "unknown"
        line_map = {}
        for market in bm.get("markets", []) or []:
            stat_key = inv_market.get(market.get("key"))
            if not stat_key:
                continue
            for outcome in market.get("outcomes", []) or []:
                side, outcome_player = _extract_player_offer_side_and_name(outcome)
                if side not in {"over", "under"}:
                    continue
                line = outcome.get("point")
                price = outcome.get("price")
                if line is None or price is None:
                    continue
                try:
                    line_val = float(line)
                    odds_val = int(price)
                except (TypeError, ValueError):
                    continue
                key = (stat_key, book_name, outcome_player, safe_round(line_val, 3))
                rec = line_map.setdefault(
                    key,
                    {
                        "stat": stat_key,
                        "bookmaker": book_name,
                        "playerName": outcome_player,
                        "line": safe_round(line_val, 3),
                        "overOdds": None,
                        "underOdds": None,
                    },
                )
                if side == "over":
                    rec["overOdds"] = odds_val
                else:
                    rec["underOdds"] = odds_val

        for rec in line_map.values():
            if rec["overOdds"] is None or rec["underOdds"] is None:
                continue
            offers_by_stat[rec["stat"]].append(rec)

    return {"success": True, "offersByStat": offers_by_stat, "payload": payload}


def _fetch_starters_with_actuals(game_id):
    def fetch():
        resp = requests.get(
            f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json",
            timeout=40,
        )
        resp.raise_for_status()
        return resp.json()

    payload = retry_api_call(fetch)
    game = payload.get("game", {}) or {}
    rows = []
    for team_key, is_home in (("homeTeam", True), ("awayTeam", False)):
        team = game.get(team_key, {}) or {}
        team_id = int(team.get("teamId", 0) or 0)
        team_abbr = str(team.get("teamTricode", "") or "").upper()
        for p in team.get("players", []) or []:
            if str(p.get("starter", "0")) != "1":
                continue
            pid = int(p.get("personId", 0) or 0)
            stats = p.get("statistics", {}) or {}
            pts = float(stats.get("points") or 0)
            reb = float(stats.get("reboundsTotal") or 0)
            ast = float(stats.get("assists") or 0)
            rows.append(
                {
                    "playerId": pid,
                    "teamId": team_id,
                    "teamAbbr": team_abbr,
                    "isHome": is_home,
                    "actual": {
                        "pts": pts,
                        "reb": reb,
                        "ast": ast,
                        "fg3m": float(stats.get("threePointersMade") or 0),
                        "stl": float(stats.get("steals") or 0),
                        "blk": float(stats.get("blocks") or 0),
                        "tov": float(stats.get("turnovers") or 0),
                        "pra": pts + reb + ast,
                    },
                }
            )
    return rows


def _grade_side(actual, line, side):
    if abs(float(actual) - float(line)) < 1e-9:
        return "push"
    if side == "over":
        return "win" if float(actual) > float(line) else "loss"
    return "win" if float(actual) < float(line) else "loss"


def _pnl_american(outcome, odds):
    o = float(odds)
    if outcome == "push":
        return 0.0
    if outcome == "loss":
        return -1.0
    return (o / 100.0) if o > 0 else (100.0 / abs(o))


def run_starter_accuracy(
    date_str=None,
    bookmakers=DEFAULT_BOOKMAKERS,
    regions=DEFAULT_REGIONS,
    sport=DEFAULT_SPORT,
    model_variant=DEFAULT_MODEL_VARIANT,
):
    """
    Evaluate pregame EV-positive leans for all starters on a date.
    """
    try:
        target_date = _parse_date(date_str) or datetime.now().date()
        target_str = target_date.isoformat()
        books = str(bookmakers or DEFAULT_BOOKMAKERS).strip()
        region_key = str(regions or DEFAULT_REGIONS).strip() or DEFAULT_REGIONS
        sport_key = str(sport or DEFAULT_SPORT).strip() or DEFAULT_SPORT
        model_key = str(model_variant or DEFAULT_MODEL_VARIANT).strip().lower() or DEFAULT_MODEL_VARIANT
        if model_key not in {"full", "simple"}:
            return {"success": False, "error": "model_variant must be 'full' or 'simple'."}

        cache_key = f"starter_accuracy_{target_str}_{books}_{region_key}_{sport_key}_{model_key}"
        cached = cache_get(cache_key, 300)
        if cached:
            cached["fromCache"] = True
            return cached

        t0 = datetime.utcnow()
        season = _season_from_date(target_date)
        prev_teams = _teams_played_on_date(target_date - timedelta(days=1))
        games = [g for g in _fetch_games_for_date(target_date) if int(g.get("gameStatus", 0)) == 3]

        by_stat = {
            stat: {"leans": 0, "wins": 0, "losses": 0, "pushes": 0, "pnlUnits": 0.0}
            for stat in TRACKED_STATS
        }
        summary = {
            "success": True,
            "targetDate": target_str,
            "bookmakers": books,
            "regions": region_key,
            "sport": sport_key,
            "modelVariant": model_key,
            "edgeMode": "ev_positive_and_threshold",
            "gamesFinal": len(games),
            "gamesWithMatchedOdds": 0,
            "startersSeen": 0,
            "starterStatMarketsWithLines": 0,
            "evLeansPlaced": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "pnlUnits": 0.0,
            "hitRateNoPushPct": None,
            "roiPctPerBet": None,
            "projectionErrors": 0,
            "missingEventOdds": 0,
            "byStat": by_stat,
            "sampleTopByEv": [],
            "sampleBottomByEv": [],
            "runtimeSec": None,
        }

        rows = []

        for gi, game in enumerate(games):
            event_id, snap_ts, event_err, _ = _event_match_id(
                game=game,
                sport=sport_key,
                regions=region_key,
                bookmakers=books,
                game_time_utc=game.get("gameTimeUTC"),
            )
            if not event_id:
                summary["missingEventOdds"] += 1
                continue
            summary["gamesWithMatchedOdds"] += 1

            offers_payload = _fetch_event_prop_offers(
                event_id=event_id,
                snapshot_ts=snap_ts,
                sport=sport_key,
                regions=region_key,
                bookmakers=books,
            )
            if not offers_payload.get("success"):
                summary["missingEventOdds"] += 1
                continue
            offers_by_stat = offers_payload.get("offersByStat", {}) or {}

            starters = _fetch_starters_with_actuals(game.get("gameId"))
            summary["startersSeen"] += len(starters)
            if gi < len(games) - 1:
                time_to_sleep = max(0.0, min(0.2, API_DELAY / 4.0))
                if time_to_sleep > 0:
                    import time as _time

                    _time.sleep(time_to_sleep)

            projection_cache = {}
            for starter in starters:
                player_id = int(starter.get("playerId", 0) or 0)
                team_id = int(starter.get("teamId", 0) or 0)
                is_home = bool(starter.get("isHome"))
                opponent_abbr = game.get("awayAbbr") if is_home else game.get("homeAbbr")
                player_name = _PLAYER_NAME_BY_ID.get(player_id, f"player_{player_id}")
                is_b2b = team_id in prev_teams

                if player_id not in projection_cache:
                    proj = compute_projection(
                        player_id=player_id,
                        opponent_abbr=opponent_abbr,
                        is_home=is_home,
                        is_b2b=is_b2b,
                        season=season,
                        as_of_date=target_str,
                        model_variant=model_key,
                    )
                    projection_cache[player_id] = proj

                proj_data = projection_cache[player_id]
                if not proj_data.get("success"):
                    summary["projectionErrors"] += 1
                    continue
                projections = proj_data.get("projections", {}) or {}

                for stat in TRACKED_STATS:
                    actual_val = (starter.get("actual") or {}).get(stat)
                    if actual_val is None:
                        continue
                    proj_stat = projections.get(stat)
                    if not proj_stat:
                        continue

                    offers = offers_by_stat.get(stat, []) or []
                    if not offers:
                        continue
                    matched = [o for o in offers if _player_name_matches(player_name, o.get("playerName", ""))]
                    if not matched:
                        continue

                    summary["starterStatMarketsWithLines"] += 1

                    projected = float(proj_stat.get("projection") or 0.0)
                    stdev_val = float(proj_stat.get("projStdev") or proj_stat.get("stdev") or 0.0)
                    best = None
                    for offer in matched:
                        line = float(offer.get("line"))
                        over_odds = int(offer.get("overOdds"))
                        under_odds = int(offer.get("underOdds"))
                        ev = compute_ev(
                            projection=projected,
                            line=line,
                            over_odds=over_odds,
                            under_odds=under_odds,
                            stdev=stdev_val,
                            stat=stat,
                        )
                        if not ev:
                            continue
                        over = ev.get("over") or {}
                        under = ev.get("under") or {}
                        over_ev = float(over.get("evPercent") or -1e9)
                        under_ev = float(under.get("evPercent") or -1e9)
                        if over_ev >= under_ev:
                            side = "over"
                            side_ev = over_ev
                            node = over
                            odds = over_odds
                        else:
                            side = "under"
                            side_ev = under_ev
                            node = under
                            odds = under_odds

                        if side_ev <= 0:
                            continue
                        if not bool(node.get("meetsThreshold")):
                            continue

                        cand = {
                            "side": side,
                            "line": line,
                            "odds": odds,
                            "bookmaker": offer.get("bookmaker"),
                            "evPct": side_ev,
                            "probOver": ev.get("probOver"),
                            "probUnder": ev.get("probUnder"),
                        }
                        if best is None or cand["evPct"] > best["evPct"]:
                            best = cand

                    if not best:
                        continue

                    outcome = _grade_side(actual_val, best["line"], best["side"])
                    pnl = _pnl_american(outcome, best["odds"])

                    summary["evLeansPlaced"] += 1
                    summary["pnlUnits"] += pnl
                    stat_bucket = summary["byStat"][stat]
                    stat_bucket["leans"] += 1
                    stat_bucket["pnlUnits"] += pnl

                    if outcome == "win":
                        summary["wins"] += 1
                        stat_bucket["wins"] += 1
                    elif outcome == "loss":
                        summary["losses"] += 1
                        stat_bucket["losses"] += 1
                    else:
                        summary["pushes"] += 1
                        stat_bucket["pushes"] += 1

                    rows.append(
                        {
                            "gameId": game.get("gameId"),
                            "playerId": player_id,
                            "playerName": player_name,
                            "teamAbbr": starter.get("teamAbbr"),
                            "opponentAbbr": opponent_abbr,
                            "isHome": is_home,
                            "isB2B": is_b2b,
                            "stat": stat,
                            "projection": safe_round(projected, 3),
                            "actual": safe_round(actual_val, 3),
                            "line": safe_round(best["line"], 3),
                            "side": best["side"],
                            "odds": int(best["odds"]),
                            "bookmaker": best.get("bookmaker"),
                            "evPct": safe_round(best["evPct"], 3),
                            "probOver": best.get("probOver"),
                            "probUnder": best.get("probUnder"),
                            "outcome": outcome,
                            "pnl1u": safe_round(pnl, 4),
                        }
                    )

        non_push = summary["wins"] + summary["losses"]
        total_bets = summary["wins"] + summary["losses"] + summary["pushes"]
        summary["pnlUnits"] = safe_round(summary["pnlUnits"], 4)
        summary["hitRateNoPushPct"] = (
            safe_round(safe_div(summary["wins"], non_push, default=0.0) * 100.0, 2) if non_push > 0 else None
        )
        summary["roiPctPerBet"] = (
            safe_round(safe_div(summary["pnlUnits"], total_bets, default=0.0) * 100.0, 2)
            if total_bets > 0
            else None
        )

        for stat in TRACKED_STATS:
            bucket = summary["byStat"][stat]
            bucket["pnlUnits"] = safe_round(bucket["pnlUnits"], 4)
            stat_non_push = bucket["wins"] + bucket["losses"]
            bucket["hitRateNoPushPct"] = (
                safe_round(safe_div(bucket["wins"], stat_non_push, default=0.0) * 100.0, 2)
                if stat_non_push > 0
                else None
            )
            bucket["roiPctPerBet"] = (
                safe_round(safe_div(bucket["pnlUnits"], bucket["leans"], default=0.0) * 100.0, 2)
                if bucket["leans"] > 0
                else None
            )

        rows.sort(key=lambda r: float(r.get("evPct", 0)), reverse=True)
        summary["sampleTopByEv"] = rows[:10]
        summary["sampleBottomByEv"] = rows[-10:] if len(rows) > 10 else list(rows)
        summary["runtimeSec"] = safe_round((datetime.utcnow() - t0).total_seconds(), 2)

        cache_set(cache_key, summary)
        return summary
    except Exception as e:
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}
