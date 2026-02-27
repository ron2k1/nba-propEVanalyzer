#!/usr/bin/env python3
"""
Multi-Book Prop Scanner (BetMGM / DraftKings / FanDuel)

Fetches live player prop lines from your configured books for today's first N NBA
games, runs model projections against each line, and ranks by model edge.

Usage:
    .venv/Scripts/python.exe scripts/betmgm_scan.py
    .venv/Scripts/python.exe scripts/betmgm_scan.py --games 8 --top 10
    .venv/Scripts/python.exe scripts/betmgm_scan.py --min-edge 0.05
    .venv/Scripts/python.exe scripts/betmgm_scan.py --books betmgm,draftkings,fanduel
    .venv/Scripts/python.exe scripts/betmgm_scan.py --skip 4 --games 4 --model full
"""

import argparse
import concurrent.futures
import math
import os
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dotenv import load_dotenv

from nba_api.stats.endpoints import leaguedashplayerstats
from nba_api.stats.static import players as nba_players_static
from nba_api.stats.static import teams as nba_teams_static_mod

from core.nba_data_collection import (
    CURRENT_SEASON,
    _extract_player_offer_side_and_name,
    _odds_api_get,
    _player_name_matches,
    _team_name_matches_abbr,
)
from core.nba_data_prep import compute_projection

load_dotenv(override=True)

SPORT = "basketball_nba"
DEFAULT_BOOKS = "betmgm,draftkings,fanduel"
REGION = "us"

# Stats to scan mapped to Odds API market keys.
SCAN_MARKETS = {
    "pts": "player_points",
    "reb": "player_rebounds",
    "ast": "player_assists",
    "fg3m": "player_threes",
    "stl": "player_steals",
    "blk": "player_blocks",
}
MARKET_TO_STAT = {v: k for k, v in SCAN_MARKETS.items()}
ALL_TEAMS = nba_teams_static_mod.get_teams()


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_over(proj, line, stdev):
    if stdev <= 0:
        return 1.0 if proj > line else 0.0
    return 1.0 - _ncdf((line - proj) / stdev)


def american_to_prob(odds):
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    return (100.0 / (o + 100.0)) if o > 0 else (abs(o) / (abs(o) + 100.0))


def novig(over_odds, under_odds):
    p_o = american_to_prob(over_odds)
    p_u = american_to_prob(under_odds)
    if p_o is None or p_u is None:
        return None, None
    total = p_o + p_u
    if total <= 0:
        return None, None
    return p_o / total, p_u / total


def _format_odds(value):
    try:
        iv = int(float(value))
    except (TypeError, ValueError):
        return "?"
    return f"+{iv}" if iv > 0 else str(iv)


def _book_priority(book_name):
    key = "".join(ch for ch in str(book_name or "").lower() if ch.isalnum())
    if "betmgm" in key:
        return 3
    if "draftkings" in key:
        return 2
    if "fanduel" in key:
        return 1
    return 0


def _team_full_to_abbr(name_str):
    for team in ALL_TEAMS:
        if _team_name_matches_abbr(name_str, team["abbreviation"]):
            return team["abbreviation"]
    return None


def fetch_player_team_map():
    """One NBA Stats API call => {'success': bool, 'map': {...}}."""
    print("  Loading season roster (NBA Stats API)...", flush=True)
    try:
        data = leaguedashplayerstats.LeagueDashPlayerStats(
            season=CURRENT_SEASON,
            per_mode_detailed="PerGame",
            measure_type_detailed_defense="Base",
        ).get_normalized_dict()
        rows = data.get("LeagueDashPlayerStats") or []
        out = {}
        for row in rows:
            team_abbr = row.get("TEAM_ABBREVIATION")
            player_id = row.get("PLAYER_ID")
            if not team_abbr or player_id is None:
                continue
            try:
                out[int(player_id)] = str(team_abbr).upper()
            except (TypeError, ValueError):
                continue
        return {"success": True, "map": out}
    except Exception as exc:
        return {"success": False, "error": str(exc), "map": {}}


def get_today_events(max_games, skip=0, books_str=DEFAULT_BOOKS):
    resp = _odds_api_get(
        f"sports/{SPORT}/odds",
        params={
            "regions": REGION,
            "markets": "h2h",
            "oddsFormat": "american",
            "dateFormat": "iso",
            "bookmakers": books_str,
        },
        timeout=30,
    )
    if not resp.get("success"):
        return [], resp.get("error"), resp.get("quota", {})

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    events = []
    for event in resp.get("data", []) or []:
        try:
            dt = datetime.fromisoformat(str(event["commence_time"]).replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except Exception:
            dt = datetime.max
        if dt < now_utc:
            continue
        event["_dt"] = dt
        events.append(event)

    events.sort(key=lambda x: x["_dt"])
    window = events[skip : skip + max(1, int(max_games or 1))]
    return window, None, resp.get("quota", {})


def fetch_props_for_event(event_id, markets_str, books_str=DEFAULT_BOOKS):
    resp = _odds_api_get(
        f"sports/{SPORT}/events/{event_id}/odds",
        params={
            "regions": REGION,
            "markets": markets_str,
            "oddsFormat": "american",
            "dateFormat": "iso",
            "bookmakers": books_str,
        },
        timeout=30,
    )
    if not resp.get("success"):
        return None, resp.get("quota", {}), resp.get("error")
    payload = resp.get("data", {}) or {}
    return payload.get("bookmakers", []) or [], resp.get("quota", {}), None


def parse_props(books):
    """Return list of tuples: (player_name, stat, line, over_odds, under_odds, sportsbook).

    One entry per book per player-stat-line combination. The main loop deduplicates
    by (player, stat, side) keeping the highest-edge result (best odds / line).
    """
    out = []
    for book in books:
        book_key = book.get("key", "unknown")
        for market in book.get("markets", []) or []:
            stat = MARKET_TO_STAT.get(market.get("key", ""))
            if not stat:
                continue

            # (player, line) => {over: odds, under: odds}
            offer_map = {}
            for outcome in market.get("outcomes", []) or []:
                side, player_name = _extract_player_offer_side_and_name(outcome)
                if side not in {"over", "under"} or not player_name:
                    continue
                try:
                    line = float(outcome["point"])
                except (KeyError, TypeError, ValueError):
                    continue
                odds = outcome.get("price")
                offer_map.setdefault((player_name, line), {})[side] = odds

            for (player_name, line), sides in offer_map.items():
                out.append((player_name, stat, line, sides.get("over"), sides.get("under"), book_key))
    return out


def _build_active_player_index():
    players = nba_players_static.get_active_players()
    idx = []
    for p in players:
        try:
            idx.append((int(p["id"]), p["full_name"]))
        except (KeyError, TypeError, ValueError):
            continue
    return idx


def _lookup_player_id(book_name, active_index):
    for pid, full_name in active_index:
        if _player_name_matches(book_name, full_name):
            return pid, full_name
    return None, None


def _project_with_timeout(player_id, opp_abbr, is_home, model_variant, timeout_sec):
    """Call compute_projection with a hard wall-clock timeout (Windows-safe via thread)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(
            compute_projection,
            player_id=player_id,
            opponent_abbr=opp_abbr,
            is_home=is_home,
            is_b2b=False,
            model_variant=model_variant,
        )
        try:
            return fut.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError:
            return {"success": False, "error": f"timeout>{timeout_sec}s"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


def main():
    parser = argparse.ArgumentParser(description="BetMGM prop scanner vs model projection")
    parser.add_argument("--games", type=int, default=4,
                        help="Number of upcoming games to scan (default: 4)")
    parser.add_argument("--skip",  type=int, default=0,
                        help="Skip first N upcoming games — for chunking, e.g. --skip 4 --games 4")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.03,
        help="Minimum model edge over BetMGM no-vig probability (default: 0.03)",
    )
    parser.add_argument(
        "--model",
        choices=["full", "simple"],
        default="full",
        help="Projection model variant to use (default: full)",
    )
    parser.add_argument(
        "--player-timeout",
        type=int,
        default=45,
        help="Hard timeout per player projection in seconds (default: 45)",
    )
    parser.add_argument(
        "--books",
        type=str,
        default=DEFAULT_BOOKS,
        help=f"Comma-separated Odds API bookmaker keys (default: {DEFAULT_BOOKS})",
    )
    args = parser.parse_args()

    markets_str = ",".join(SCAN_MARKETS.values())

    books_label = args.books.upper().replace(",", " / ")
    print("=" * 72, flush=True)
    print(f"PROP SCANNER [{books_label}]: live lines vs model projection", flush=True)
    print("=" * 72, flush=True)

    print("\n[1/4] Building player->team map...", flush=True)
    roster_data = fetch_player_team_map()
    if not roster_data.get("success"):
        print(f"  ERROR: failed to load NBA roster data: {roster_data.get('error')}", flush=True)
        print("  Check internet access/firewall and retry.", flush=True)
        return
    pid_to_team = roster_data.get("map", {})
    active_index = _build_active_player_index()
    print(f"  Loaded {len(pid_to_team)} rostered players.", flush=True)
    print(f"  Loaded {len(active_index)} active players for name matching.", flush=True)

    skip_str = f"  (skipping first {args.skip})" if args.skip else ""
    print(f"\n[2/4] Fetching upcoming events (next {args.games}){skip_str}...", flush=True)
    events, err, quota = get_today_events(args.games, args.skip, args.books)
    if not events:
        print(f"  ERROR / no events: {err or 'none'}", flush=True)
        if quota:
            print(f"  Quota: {quota}", flush=True)
        return

    game_meta = []
    for event in events:
        home_abbr = _team_full_to_abbr(event.get("home_team", "")) or event.get("home_team", "?")[:3].upper()
        away_abbr = _team_full_to_abbr(event.get("away_team", "")) or event.get("away_team", "?")[:3].upper()
        dt = event.get("_dt")
        time_str = dt.strftime("%I:%M %p") if dt and dt != datetime.max else "?"
        print(f"  {away_abbr} @ {home_abbr} ({time_str} ET)", flush=True)
        game_meta.append((home_abbr, away_abbr, event.get("id")))

    print(f"\n[3/4] Fetching player props per game ({args.books})...", flush=True)
    all_offers = []
    last_quota = quota or {}
    for i, (home_abbr, away_abbr, event_id) in enumerate(game_meta, 1):
        if not event_id:
            continue
        books, q, fetch_err = fetch_props_for_event(event_id, markets_str, args.books)
        if q:
            last_quota = q
        if books is None:
            print(f"  [{i}/{len(game_meta)}] {away_abbr}@{home_abbr}: ERROR - {fetch_err}", flush=True)
            continue
        props = parse_props(books)
        for offer in props:
            all_offers.append((home_abbr, away_abbr) + offer)
        print(
            f"  [{i}/{len(game_meta)}] {away_abbr}@{home_abbr}: {len(props)} props "
            f"(quota remaining: {last_quota.get('remaining', '?')})",
            flush=True,
        )

    total_props = len(all_offers)
    print(f"\n  Total props to evaluate: {total_props}", flush=True)
    if total_props == 0:
        print("  No props found. BetMGM may not have posted player lines yet.", flush=True)
        return

    print(
        f"\n[4/4] Running projections  model={args.model}  timeout={args.player_timeout}s...",
        flush=True,
    )

    # Cache projection per unique player+game context so we do not call NBA APIs per prop line.
    projection_cache = {}

    results = []
    skipped = 0
    no_match = 0
    missing_stat = 0
    timeouts = 0

    for idx, (home_abbr, away_abbr, book_name, stat, line, over_odds, under_odds, sportsbook) in enumerate(all_offers, 1):
        if idx % 50 == 0:
            print(f"  [{idx}/{total_props}] ...", flush=True)

        player_id, canonical_name = _lookup_player_id(book_name, active_index)
        if player_id is None:
            no_match += 1
            continue

        player_team = pid_to_team.get(player_id)
        if not player_team:
            skipped += 1
            continue

        if player_team == home_abbr:
            opp_abbr = away_abbr
            is_home = True
        elif player_team == away_abbr:
            opp_abbr = home_abbr
            is_home = False
        else:
            # Team mismatch can happen with recent trades / stale roster snapshot.
            skipped += 1
            continue

        cache_key = (player_id, opp_abbr, is_home, args.model)
        if cache_key not in projection_cache:
            payload = _project_with_timeout(
                player_id, opp_abbr, is_home, args.model, args.player_timeout
            )
            if payload.get("error", "").startswith("timeout"):
                timeouts += 1
            projection_cache[cache_key] = payload

        proj_payload = projection_cache.get(cache_key) or {}
        if not proj_payload.get("success"):
            skipped += 1
            continue

        stat_proj = (proj_payload.get("projections") or {}).get(stat)
        if not stat_proj:
            missing_stat += 1
            continue

        try:
            projection = float(stat_proj.get("projection"))
        except (TypeError, ValueError):
            projection = float(line)
        stdev = stat_proj.get("projStdev", stat_proj.get("stdev"))
        try:
            stdev = float(stdev)
        except (TypeError, ValueError):
            stdev = 0.0
        if stdev <= 0:
            skipped += 1
            continue

        p_model_over = prob_over(projection, line, stdev)
        p_model_under = 1.0 - p_model_over

        if over_odds is not None and under_odds is not None:
            nv_over, nv_under = novig(over_odds, under_odds)
        elif over_odds is not None:
            nv_over = american_to_prob(over_odds)
            nv_under = (1.0 - nv_over) if nv_over is not None else None
        else:
            nv_over, nv_under = None, None

        if nv_over is None:
            skipped += 1
            continue
        if nv_under is None:
            nv_under = 1.0 - nv_over

        edge_over = p_model_over - nv_over
        edge_under = p_model_under - nv_under

        if edge_over >= edge_under and edge_over >= args.min_edge:
            best_side = "OVER"
            best_edge = edge_over
            best_odds = over_odds
            best_prob = p_model_over
            best_nv = nv_over
        elif edge_under > edge_over and edge_under >= args.min_edge:
            best_side = "UNDER"
            best_edge = edge_under
            best_odds = under_odds
            best_prob = p_model_under
            best_nv = nv_under
        else:
            continue

        results.append(
            {
                "player": canonical_name,
                "team": player_team,
                "opp": opp_abbr,
                "game": f"{away_abbr}@{home_abbr}",
                "stat": stat.upper(),
                "line": float(line),
                "side": best_side,
                "book": sportsbook,
                "odds": best_odds,
                "proj": round(projection, 1),
                "stdev": round(stdev, 2),
                "prob": round(best_prob * 100.0, 1),
                "nv_implied": round(best_nv * 100.0, 1),
                "edge": round(best_edge * 100.0, 1),
            }
        )

    # Deduplicate: for the same player×stat×side keep only the best-edge entry
    # (multiple books posting the same prop → bet the one with highest edge).
    best_per_play: dict = {}
    for row in results:
        key = (row["player"], row["stat"], row["side"])
        if key not in best_per_play:
            best_per_play[key] = row
            continue
        current = best_per_play[key]
        if row["edge"] > current["edge"]:
            best_per_play[key] = row
            continue
        if row["edge"] == current["edge"] and _book_priority(row.get("book")) > _book_priority(current.get("book")):
            best_per_play[key] = row
    results = sorted(best_per_play.values(), key=lambda x: -x["edge"])
    print(
        f"  Done. {len(results)} value plays  ·  "
        f"{no_match} name misses  ·  {timeouts} timeouts  ·  "
        f"{missing_stat} missing-stat  ·  {skipped} skipped",
        flush=True,
    )
    print(f"  Projection cache: {len(projection_cache)} unique player×game lookups", flush=True)
    if last_quota:
        print(
            f"  Quota remaining={last_quota.get('remaining')} used={last_quota.get('used')} last={last_quota.get('last')}",
            flush=True,
        )

    if not results:
        print("\nNo value plays found above threshold.", flush=True)
        return

    print("\n" + "=" * 72, flush=True)
    print(
        f"TOP {min(args.top, len(results))} VALUE PLAYS  {datetime.now().strftime('%Y-%m-%d')}",
        flush=True,
    )
    print(
        f"Books: {args.books}   |   edge >= {args.min_edge * 100.0:.1f}%   |   model: {args.model}",
        flush=True,
    )
    print("=" * 72, flush=True)

    for rank, row in enumerate(results[: args.top], 1):
        print(
            f"\n{rank:>2}. {row['player']}  {row['side']} {row['line']} {row['stat']} "
            f"({_format_odds(row['odds'])})  [{row['game']}]  @{row['book']}",
            flush=True,
        )
        print(
            f"    proj {row['proj']:.1f} | model {row['prob']:.1f}% vs book {row['nv_implied']:.1f}% "
            f"| edge +{row['edge']:.1f}% | stdev {row['stdev']:.2f}",
            flush=True,
        )


if __name__ == "__main__":
    main()
