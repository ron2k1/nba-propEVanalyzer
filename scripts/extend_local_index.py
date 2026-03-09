#!/usr/bin/env python3
"""
Extend the local NBA stats index (index.pkl) with recent games from the NBA API.
Fetches scoreboard + boxscores for each missing date and appends to the pickle.

Usage:
    .venv/Scripts/python.exe scripts/extend_local_index.py --through 2026-03-05
    .venv/Scripts/python.exe scripts/extend_local_index.py --through 2026-03-05 --dry-run
"""

import argparse
import os
import pickle
import sys
import time
import uuid
from datetime import date, datetime, timedelta

# Add repo root to path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from nba_api.stats.endpoints import scoreboardv3, boxscoretraditionalv3
from nba_api.stats.static import teams as nba_teams_static

HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.nba.com/",
    "Accept": "application/json",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}
API_DELAY = 0.8
INDEX_PATH = os.path.join(_ROOT, "data", "reference", "kaggle_nba", "index.pkl")


def _safe_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _safe_round(v, n=1):
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return None


def _safe_div(a, b):
    return a / b if b else 0.0


def _pct_out(v):
    x = v
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return _safe_round(x * 100.0, 1) if x <= 1.0 else _safe_round(x, 1)


def _season_label(date_str):
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        y = d.year
        if d.month >= 10:
            return f"{y}-{str(y + 1)[-2:]}"
        return f"{y - 1}-{str(y)[-2:]}"
    except ValueError:
        return "unknown"


def _month_abbr(m):
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m - 1]


def _format_game_date(date_str):
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        return f"{_month_abbr(d.month)} {d.day}, {d.year}"
    except ValueError:
        return str(date_str)


def _build_team_map():
    return {
        int(t.get("id", 0) or 0): str(t.get("abbreviation", "") or "").upper()
        for t in nba_teams_static.get_teams()
    }


def _fetch_scoreboard(date_str):
    data = scoreboardv3.ScoreboardV3(
        game_date=date_str, league_id="00", timeout=30
    ).get_dict()
    return data.get("scoreboard", {}).get("games", [])


def _fetch_boxscore(game_id):
    box = boxscoretraditionalv3.BoxScoreTraditionalV3(
        game_id=game_id, timeout=30
    ).get_dict()
    resource = box.get("boxScoreTraditional", {})
    home = resource.get("homeTeam", {})
    away = resource.get("awayTeam", {})
    return home, away


def _atomic_pickle_dump(path, payload):
    directory = os.path.dirname(path) or "."
    tmp_path = os.path.join(directory, f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp")
    with open(tmp_path, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp_path, path)


def _upsert_game_entry(games_by_date, game_entry):
    date_str = str(game_entry.get("gameDate") or "")
    game_id = str(game_entry.get("gameId") or "")
    if not date_str or not game_id:
        return "skipped"

    day_games = games_by_date.setdefault(date_str, [])
    for idx, existing in enumerate(day_games):
        if str(existing.get("gameId") or "") != game_id:
            continue
        if existing == game_entry:
            return "unchanged"
        day_games[idx] = game_entry
        return "updated"

    day_games.append(game_entry)
    return "added"


def _upsert_boxscore_row(boxscore_by_game, game_id, row):
    gid = str(game_id or "")
    player_id = _safe_int(row.get("PLAYER_ID"), 0)
    team_id = _safe_int(row.get("TEAM_ID"), 0)
    if not gid or player_id <= 0 or team_id <= 0:
        return "skipped"

    rows = boxscore_by_game.setdefault(gid, [])
    for idx, existing in enumerate(rows):
        if (
            _safe_int(existing.get("PLAYER_ID"), 0) == player_id
            and _safe_int(existing.get("TEAM_ID"), 0) == team_id
        ):
            if existing == row:
                return "unchanged"
            rows[idx] = row
            return "updated"

    rows.append(row)
    return "added"


def _upsert_gamelog_row(gamelogs_by_player, player_id, row):
    pid = _safe_int(player_id, 0)
    game_id = str(row.get("gameId") or "")
    if pid <= 0 or not game_id:
        return "skipped"

    rows = gamelogs_by_player.setdefault(pid, [])
    for idx, existing in enumerate(rows):
        if str(existing.get("gameId") or "") != game_id:
            continue
        if existing == row:
            return "unchanged"
        rows[idx] = row
        return "updated"

    rows.append(row)
    return "added"


def extend_index(through_date, dry_run=False):
    print(f"Loading index from {INDEX_PATH} ...", flush=True)
    with open(INDEX_PATH, "rb") as fh:
        index = pickle.load(fh)

    games_by_date = index.get("games_by_date", {})
    boxscore_by_game = index.get("boxscore_by_game", {})
    gamelogs_by_player = index.get("gamelogs_by_player", {})
    team_map = index.get("team_id_to_abbr", {})
    seasons_set = set(index.get("seasons_covered", []))
    current_max = index.get("max_date", "")

    # Supplement team_map with nba_api static
    for tid, abbr in _build_team_map().items():
        team_map.setdefault(tid, abbr)

    print(f"Current max_date: {current_max}", flush=True)
    print(f"Target through:   {through_date}", flush=True)

    if current_max >= through_date:
        print("Index already covers through target date. Nothing to do.")
        return

    # Generate list of dates to fetch
    start = date.fromisoformat(current_max) + timedelta(days=1)
    end = date.fromisoformat(through_date)
    dates = []
    d = start
    while d <= end:
        dates.append(d.isoformat())
        d += timedelta(days=1)

    print(f"Dates to process: {len(dates)} ({dates[0]} -> {dates[-1]})", flush=True)
    if dry_run:
        print("DRY RUN — not fetching or saving.")
        return

    total_games_scanned = 0
    total_players_processed = 0
    update_counts = {
        "gamesAdded": 0,
        "gamesUpdated": 0,
        "boxRowsAdded": 0,
        "boxRowsUpdated": 0,
        "gameLogsAdded": 0,
        "gameLogsUpdated": 0,
    }

    for date_str in dates:
        print(f"\n  {date_str}: ", end="", flush=True)
        try:
            raw_games = _fetch_scoreboard(date_str)
        except Exception as e:
            print(f"ERROR fetching scoreboard: {e}")
            time.sleep(API_DELAY)
            continue

        if not raw_games:
            print("no games", flush=True)
            time.sleep(API_DELAY)
            continue

        # Filter to completed games only
        completed = [g for g in raw_games if g.get("gameStatus", 1) == 3]
        if not completed:
            print(f"{len(raw_games)} games found but none completed", flush=True)
            time.sleep(API_DELAY)
            continue

        print(f"{len(completed)} games ", end="", flush=True)
        total_games_scanned += len(completed)

        for g in completed:
            game_id = str(g.get("gameId", "")).zfill(10)
            home_team = g.get("homeTeam", {})
            away_team = g.get("awayTeam", {})
            home_id = _safe_int(home_team.get("teamId"), 0)
            away_id = _safe_int(away_team.get("teamId"), 0)
            home_pts = _safe_int(home_team.get("score"), 0)
            away_pts = _safe_int(away_team.get("score"), 0)
            home_abbr = team_map.get(home_id, home_team.get("teamTricode", ""))
            away_abbr = team_map.get(away_id, away_team.get("teamTricode", ""))

            game_entry = {
                "gameId": game_id,
                "homeTeamId": home_id,
                "awayTeamId": away_id,
                "homeAbbr": home_abbr,
                "awayAbbr": away_abbr,
                "homePts": home_pts,
                "awayPts": away_pts,
                "gameDate": date_str,
            }
            game_status = _upsert_game_entry(games_by_date, game_entry)
            if game_status == "added":
                update_counts["gamesAdded"] += 1
            elif game_status == "updated":
                update_counts["gamesUpdated"] += 1
            seasons_set.add(_season_label(date_str))

            # Fetch boxscore
            time.sleep(API_DELAY)
            try:
                home_box, away_box = _fetch_boxscore(game_id)
            except Exception as e:
                print(f"[box err {game_id}: {e}] ", end="", flush=True)
                continue

            for side_data, is_home in [(home_box, True), (away_box, False)]:
                players = side_data.get("players", [])
                for p in players:
                    stats = p.get("statistics", {})
                    mins_raw = str(stats.get("minutes", "") or "").strip()
                    # Parse PT35M20.00S format
                    mins = 0.0
                    if mins_raw.startswith("PT"):
                        import re
                        m = re.match(r"PT(\d+)M([\d.]+)S", mins_raw)
                        if m:
                            mins = float(m.group(1)) + float(m.group(2)) / 60.0
                    elif ":" in mins_raw:
                        parts = mins_raw.split(":")
                        try:
                            mins = float(parts[0]) + float(parts[1]) / 60.0
                        except (ValueError, IndexError):
                            pass
                    else:
                        try:
                            mins = float(mins_raw)
                        except (TypeError, ValueError):
                            pass

                    if mins <= 0:
                        continue

                    pid = _safe_int(p.get("personId"), 0)
                    if not pid:
                        continue

                    tid = home_id if is_home else away_id
                    opp_id = away_id if is_home else home_id
                    opp_abbr = team_map.get(opp_id, "")
                    my_abbr = team_map.get(tid, "")
                    matchup = f"{my_abbr} vs. {opp_abbr}" if is_home else f"{my_abbr} @ {opp_abbr}"
                    wl = "W" if (is_home and home_pts > away_pts) or (not is_home and away_pts > home_pts) else "L"
                    season = _season_label(date_str)

                    pts = _safe_int(stats.get("points"), 0)
                    reb = _safe_int(stats.get("reboundsTotal"), 0)
                    ast = _safe_int(stats.get("assists"), 0)
                    stl = _safe_int(stats.get("steals"), 0)
                    blk = _safe_int(stats.get("blocks"), 0)
                    tov = _safe_int(stats.get("turnovers"), 0)
                    fg3m = _safe_int(stats.get("threePointersMade"), 0)
                    fg3a = _safe_int(stats.get("threePointersAttempted"), 0)
                    fgm = _safe_int(stats.get("fieldGoalsMade"), 0)
                    fga = _safe_int(stats.get("fieldGoalsAttempted"), 0)
                    ftm = _safe_int(stats.get("freeThrowsMade"), 0)
                    fta = _safe_int(stats.get("freeThrowsAttempted"), 0)
                    plus_minus = _safe_int(stats.get("plusMinusPoints"), 0)

                    box_row = {
                        "PLAYER_ID": pid,
                        "TEAM_ID": tid,
                        "MIN": mins_raw,
                        "PTS": pts,
                        "REB": reb,
                        "AST": ast,
                        "STL": stl,
                        "BLK": blk,
                        "TOV": tov,
                        "FG3M": fg3m,
                    }
                    box_status = _upsert_boxscore_row(boxscore_by_game, game_id, box_row)
                    if box_status == "added":
                        update_counts["boxRowsAdded"] += 1
                    elif box_status == "updated":
                        update_counts["boxRowsUpdated"] += 1

                    gamelog_row = {
                        "gameDate": _format_game_date(date_str),
                        "gameId": game_id,
                        "matchup": matchup,
                        "opponent": opp_abbr,
                        "isHome": is_home,
                        "wl": wl,
                        "min": _safe_round(mins, 1),
                        "pts": pts,
                        "reb": reb,
                        "ast": ast,
                        "stl": stl,
                        "blk": blk,
                        "tov": tov,
                        "fg3m": fg3m,
                        "fg3a": fg3a,
                        "fgm": fgm,
                        "fga": fga,
                        "ftm": ftm,
                        "fta": fta,
                        "fgPct": _pct_out(stats.get("fieldGoalsPercentage")),
                        "fg3Pct": _pct_out(stats.get("threePointersPercentage")),
                        "ftPct": _pct_out(stats.get("freeThrowsPercentage")),
                        "plusMinus": plus_minus,
                        "pra": pts + reb + ast,
                        "pr": pts + reb,
                        "pa": pts + ast,
                        "ra": reb + ast,
                        "stocksBlkStl": stl + blk,
                        "_date_str": date_str,
                        "_team_id": tid,
                        "_team_abbr": my_abbr,
                        "season": season,
                    }
                    log_status = _upsert_gamelog_row(gamelogs_by_player, pid, gamelog_row)
                    if log_status == "added":
                        update_counts["gameLogsAdded"] += 1
                    elif log_status == "updated":
                        update_counts["gameLogsUpdated"] += 1
                    total_players_processed += 1

        time.sleep(API_DELAY)

    # Re-sort gamelogs for players that got new entries
    print(f"\n\nSorting gamelogs ...", flush=True)
    for pid in gamelogs_by_player:
        gamelogs_by_player[pid].sort(key=lambda g: g.get("_date_str", ""))

    # Update metadata
    new_max = through_date
    index["games_by_date"] = games_by_date
    index["boxscore_by_game"] = boxscore_by_game
    index["gamelogs_by_player"] = gamelogs_by_player
    index["team_id_to_abbr"] = team_map
    index["seasons_covered"] = sorted(seasons_set)
    index["max_date"] = new_max

    print(f"Saving updated index atomically ...", flush=True)
    _atomic_pickle_dump(INDEX_PATH, index)

    size_mb = os.path.getsize(INDEX_PATH) / (1024 * 1024)
    print(
        f"\nDone: scanned {total_games_scanned} games, processed {total_players_processed} player rows",
        flush=True,
    )
    print(
        "Changes: "
        f"games +{update_counts['gamesAdded']} / ~{update_counts['gamesUpdated']} updated, "
        f"box +{update_counts['boxRowsAdded']} / ~{update_counts['boxRowsUpdated']} updated, "
        f"logs +{update_counts['gameLogsAdded']} / ~{update_counts['gameLogsUpdated']} updated",
        flush=True,
    )
    print(f"New max_date: {new_max}")
    print(f"Index size: {size_mb:.0f} MB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extend local NBA stats index with recent API data")
    parser.add_argument("--through", required=True, help="Extend index through this date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without doing it")
    args = parser.parse_args()
    extend_index(args.through, dry_run=args.dry_run)
