#!/usr/bin/env python3
"""
Build local NBA backtest index from CSV data.

Supported input schemas:
1) Legacy (nathanlauga/nba-games)
   - games.csv
   - games_details.csv
   - teams.csv
2) Eoin Moore (NBA Database 1947-Present)
   - Games.csv
   - PlayerStatistics.csv
   - TeamHistories.csv
   - optional: Players.csv (for position hints)

Usage:
    .venv/Scripts/python.exe scripts/index_local_data.py
    .venv/Scripts/python.exe scripts/index_local_data.py --input-dir "NBA Database (1947 - Present)"
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import pickle
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime

from nba_api.stats.static import teams as nba_teams_static


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


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


def _to_bool(v):
    s = str(v or "").strip().lower()
    return s in {"1", "true", "t", "yes", "y"}


def _normalize_game_id(raw_gid):
    s = str(raw_gid or "").strip()
    if not s:
        return ""
    try:
        return str(int(float(s))).zfill(10)
    except (TypeError, ValueError):
        digits = re.sub(r"\D+", "", s)
        if digits:
            return digits.zfill(10)
        return s


def _month_abbr(m):
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m - 1]


def _parse_iso_date(raw):
    s = str(raw or "").strip()
    if not s:
        return ""
    # Common case: "YYYY-MM-DD..." (datetime/timezone suffixes)
    if len(s) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _format_game_date(date_str):
    """YYYY-MM-DD -> 'Jan 15, 2023' to match NBA API style."""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        return f"{_month_abbr(d.month)} {d.day}, {d.year}"
    except ValueError:
        return str(date_str)


def _season_label(date_str):
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        y = d.year
        if d.month >= 10:
            return f"{y}-{str(y + 1)[-2:]}"
        return f"{y - 1}-{str(y)[-2:]}"
    except ValueError:
        return "unknown"


def _parse_min(raw):
    """
    Convert minute formats to float minutes:
    - MM:SS
    - float string
    - ISO duration (PT35M20.00S)
    """
    s = str(raw or "").strip()
    if not s or s.upper() in {"DNP", "DND", "NWT", "N/A"}:
        return 0.0

    m = re.match(r"PT(\d+)M([\d.]+)S", s)
    if m:
        return float(m.group(1)) + float(m.group(2)) / 60.0

    if ":" in s:
        mm, ss = s.split(":", 1)
        try:
            return max(0.0, float(mm) + float(ss) / 60.0)
        except (TypeError, ValueError):
            return 0.0

    try:
        return max(0.0, float(s))
    except (TypeError, ValueError):
        return 0.0


def _pct_out(v):
    x = _safe_float(v, None)
    if x is None:
        return None
    return _safe_round(x * 100.0, 1) if x <= 1.0 else _safe_round(x, 1)


def _iter_csv(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield row


def _rolling_stats(logs, key):
    vals = [_safe_float(g.get(key, 0)) for g in logs]
    n = len(vals)
    avg5 = _safe_round(statistics.mean(vals[:5]), 1) if n >= 5 else (_safe_round(statistics.mean(vals), 1) if vals else 0)
    avg10 = _safe_round(statistics.mean(vals[:10]), 1) if n >= 10 else (_safe_round(statistics.mean(vals), 1) if vals else 0)
    avg_s = _safe_round(statistics.mean(vals), 1) if vals else 0
    med = _safe_round(statistics.median(vals), 1) if vals else 0
    std = _safe_round(statistics.stdev(vals), 2) if n >= 2 else 0
    mn = min(vals) if vals else 0
    mx = max(vals) if vals else 0
    return avg5, avg10, avg_s, med, std, mn, mx


def _compute_rolling(logs):
    keys = ["pts", "reb", "ast", "stl", "blk", "tov", "fg3m", "pra", "pr", "pa", "ra", "min"]
    rolling = {}
    for key in keys:
        avg5, avg10, avg_s, med, std, mn, mx = _rolling_stats(logs, key)
        rolling[f"{key}_avg5"] = avg5
        rolling[f"{key}_avg10"] = avg10
        rolling[f"{key}_avg_season"] = avg_s
        rolling[f"{key}_median"] = med
        rolling[f"{key}_stdev"] = std
        rolling[f"{key}_min"] = mn
        rolling[f"{key}_max"] = mx
    return rolling


def _compute_hit_rates(logs):
    hit_rates = {}
    for key in ["pts", "reb", "ast", "fg3m", "pra", "stl", "blk", "tov"]:
        vals = [_safe_float(g.get(key, 0)) for g in logs]
        if not vals:
            continue
        avg = statistics.mean(vals)
        primary_line = math.floor(avg) + 0.5
        alt_lines = {}
        for offset in [-3.0, -1.5, 0.0, 1.5, 3.0]:
            line = max(0.5, primary_line + offset)
            over_count = sum(1 for v in vals if v > line)
            alt_lines[str(line)] = _safe_round(_safe_div(over_count, len(vals)) * 100, 1)
        over_primary = sum(1 for v in vals if v > primary_line)
        under_primary = len(vals) - over_primary
        hit_rates[key] = {
            "line": primary_line,
            "overRate": _safe_round(_safe_div(over_primary, len(vals)) * 100, 1),
            "underRate": _safe_round(_safe_div(under_primary, len(vals)) * 100, 1),
            "sampleSize": len(vals),
            "avg": _safe_round(avg, 1),
            "altLines": alt_lines,
        }
    return hit_rates


def _detect_schema(input_dir):
    legacy = all(
        os.path.exists(os.path.join(input_dir, name))
        for name in ("games.csv", "games_details.csv", "teams.csv")
    )
    eoin = all(
        os.path.exists(os.path.join(input_dir, name))
        for name in ("Games.csv", "PlayerStatistics.csv", "TeamHistories.csv")
    )
    if legacy:
        return "legacy"
    if eoin:
        return "eoin"
    return None


def _load_team_map_legacy(path):
    team_id_to_abbr = {}
    for row in _iter_csv(path):
        tid = _safe_int(row.get("TEAM_ID") or row.get("id"), 0)
        abbr = str(row.get("ABBREVIATION") or row.get("abbreviation") or "").upper().strip()
        if tid and abbr:
            team_id_to_abbr[tid] = abbr
    return team_id_to_abbr


def _load_team_map_eoin(path):
    nba_static = {
        int(t.get("id", 0) or 0): str(t.get("abbreviation", "") or "").upper()
        for t in nba_teams_static.get_teams()
    }
    best = {}
    for row in _iter_csv(path):
        tid = _safe_int(row.get("teamId") or row.get("TEAM_ID"), 0)
        if not tid:
            continue
        abbr = str(row.get("teamAbbrev") or row.get("ABBREVIATION") or "").upper().strip()
        season_till = _safe_int(row.get("seasonActiveTill"), 0)
        rank_key = season_till if season_till else -1
        prev = best.get(tid)
        if prev is None or rank_key >= prev[0]:
            best[tid] = (rank_key, abbr)

    out = {}
    for tid, (_, abbr) in best.items():
        out[tid] = abbr or nba_static.get(tid, "")
    for tid, abbr in nba_static.items():
        out.setdefault(tid, abbr)
    return out


def _derive_position(guard, forward, center):
    g = _to_bool(guard)
    f = _to_bool(forward)
    c = _to_bool(center)
    if c and not g and not f:
        return "C"
    if g and not f and not c:
        return "G"
    if f and not g and not c:
        return "F"
    if c:
        return "C"
    if f:
        return "F"
    if g:
        return "G"
    return None


def _load_player_positions(path):
    if not os.path.exists(path):
        return {}
    positions = {}
    for row in _iter_csv(path):
        pid = _safe_int(row.get("personId") or row.get("PLAYER_ID"), 0)
        if not pid:
            continue
        pos = _derive_position(
            row.get("guard"),
            row.get("forward"),
            row.get("center"),
        )
        if pos:
            positions[pid] = pos
    return positions


def _append_game_entry(games_by_date, game_meta, seasons_set, team_id_to_abbr, game_id, date_str, home_id, away_id, home_pts, away_pts):
    if not game_id or not date_str or not home_id or not away_id:
        return

    home_abbr = team_id_to_abbr.get(home_id, "")
    away_abbr = team_id_to_abbr.get(away_id, "")
    entry = {
        "gameId": game_id,
        "homeTeamId": home_id,
        "awayTeamId": away_id,
        "homeAbbr": home_abbr,
        "awayAbbr": away_abbr,
        "homePts": home_pts,
        "awayPts": away_pts,
        "gameDate": date_str,
    }
    games_by_date[date_str].append(entry)
    game_meta[game_id] = entry
    seasons_set.add(_season_label(date_str))


def _build_games_legacy(games_csv, team_id_to_abbr):
    games_by_date = defaultdict(list)
    game_meta = {}
    seasons_set = set()
    row_count = 0

    for row in _iter_csv(games_csv):
        row_count += 1
        game_id = _normalize_game_id(row.get("GAME_ID") or row.get("game_id"))
        date_str = _parse_iso_date(row.get("GAME_DATE_EST") or row.get("game_date_est"))
        home_id = _safe_int(row.get("HOME_TEAM_ID") or row.get("home_team_id"), 0)
        away_id = _safe_int(row.get("VISITOR_TEAM_ID") or row.get("visitor_team_id"), 0)
        home_pts = _safe_int(row.get("PTS_home") or row.get("pts_home"), 0)
        away_pts = _safe_int(row.get("PTS_away") or row.get("pts_away"), 0)
        _append_game_entry(
            games_by_date,
            game_meta,
            seasons_set,
            team_id_to_abbr,
            game_id,
            date_str,
            home_id,
            away_id,
            home_pts,
            away_pts,
        )
    return games_by_date, game_meta, seasons_set, row_count


def _build_games_eoin(games_csv, team_id_to_abbr):
    games_by_date = defaultdict(list)
    game_meta = {}
    seasons_set = set()
    row_count = 0

    for row in _iter_csv(games_csv):
        row_count += 1
        game_id = _normalize_game_id(row.get("gameId") or row.get("GAME_ID"))
        date_str = _parse_iso_date(row.get("gameDateTimeEst") or row.get("GAME_DATE_EST"))
        home_id = _safe_int(row.get("hometeamId") or row.get("HOME_TEAM_ID"), 0)
        away_id = _safe_int(row.get("awayteamId") or row.get("VISITOR_TEAM_ID"), 0)
        home_pts = _safe_int(row.get("homeScore") or row.get("PTS_home"), 0)
        away_pts = _safe_int(row.get("awayScore") or row.get("PTS_away"), 0)
        _append_game_entry(
            games_by_date,
            game_meta,
            seasons_set,
            team_id_to_abbr,
            game_id,
            date_str,
            home_id,
            away_id,
            home_pts,
            away_pts,
        )
    return games_by_date, game_meta, seasons_set, row_count


def _add_box_and_log_row(
    row_count,
    boxscore_by_game,
    gamelogs_by_player,
    team_id_to_abbr,
    meta,
    game_id,
    pid,
    team_id,
    mins_raw,
    mins,
    pts,
    reb,
    ast,
    stl,
    blk,
    tov,
    fg3m,
    fg3a,
    fgm,
    fga,
    ftm,
    fta,
    fg_pct_out,
    fg3_pct_out,
    ft_pct_out,
    plus_minus,
    wl_hint=None,
    is_home_hint=None,
):
    if mins <= 0:
        return row_count

    is_home = bool(is_home_hint) if is_home_hint is not None else (team_id == meta["homeTeamId"])
    team_abbr = team_id_to_abbr.get(team_id, "")
    opp_id = meta["awayTeamId"] if is_home else meta["homeTeamId"]
    opp_abbr = team_id_to_abbr.get(opp_id, "")
    matchup = f"{team_abbr} vs. {opp_abbr}" if is_home else f"{team_abbr} @ {opp_abbr}"
    date_str = meta["gameDate"]
    season = _season_label(date_str)

    if wl_hint in {"W", "L"}:
        wl = wl_hint
    else:
        if is_home:
            wl = "W" if meta["homePts"] > meta["awayPts"] else "L"
        else:
            wl = "W" if meta["awayPts"] > meta["homePts"] else "L"

    boxscore_by_game[game_id].append(
        {
            "PLAYER_ID": pid,
            "TEAM_ID": team_id,
            "MIN": mins_raw,
            "PTS": pts,
            "REB": reb,
            "AST": ast,
            "STL": stl,
            "BLK": blk,
            "TOV": tov,
            "FG3M": fg3m,
        }
    )

    gamelogs_by_player[pid].append(
        {
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
            "fgPct": fg_pct_out,
            "fg3Pct": fg3_pct_out,
            "ftPct": ft_pct_out,
            "plusMinus": plus_minus,
            "pra": pts + reb + ast,
            "pr": pts + reb,
            "pa": pts + ast,
            "ra": reb + ast,
            "stocksBlkStl": stl + blk,
            "_date_str": date_str,
            "_team_id": team_id,
            "_team_abbr": team_abbr,
            "season": season,
        }
    )
    return row_count + 1


def _build_player_rows_legacy(details_csv, game_meta, team_id_to_abbr):
    boxscore_by_game = defaultdict(list)
    gamelogs_by_player = defaultdict(list)
    kept_rows = 0
    scanned_rows = 0

    for row in _iter_csv(details_csv):
        scanned_rows += 1
        game_id = _normalize_game_id(row.get("GAME_ID") or row.get("game_id"))
        meta = game_meta.get(game_id)
        if not meta:
            continue

        pid = _safe_int(row.get("PLAYER_ID") or row.get("player_id"), 0)
        team_id = _safe_int(row.get("TEAM_ID") or row.get("team_id"), 0)
        if not pid or not team_id:
            continue

        mins_raw = str(row.get("MIN") or row.get("min") or "").strip()
        mins = _parse_min(mins_raw)
        if mins <= 0:
            continue

        kept_rows = _add_box_and_log_row(
            kept_rows,
            boxscore_by_game,
            gamelogs_by_player,
            team_id_to_abbr,
            meta,
            game_id,
            pid,
            team_id,
            mins_raw,
            mins,
            _safe_int(row.get("PTS") or row.get("pts"), 0),
            _safe_int(row.get("REB") or row.get("reb"), 0),
            _safe_int(row.get("AST") or row.get("ast"), 0),
            _safe_int(row.get("STL") or row.get("stl"), 0),
            _safe_int(row.get("BLK") or row.get("blk"), 0),
            _safe_int(row.get("TO") or row.get("TOV") or row.get("to") or row.get("tov"), 0),
            _safe_int(row.get("FG3M") or row.get("fg3m"), 0),
            _safe_int(row.get("FG3A") or row.get("fg3a"), 0),
            _safe_int(row.get("FGM") or row.get("fgm"), 0),
            _safe_int(row.get("FGA") or row.get("fga"), 0),
            _safe_int(row.get("FTM") or row.get("ftm"), 0),
            _safe_int(row.get("FTA") or row.get("fta"), 0),
            _pct_out(row.get("FG_PCT") or row.get("fg_pct")),
            _pct_out(row.get("FG3_PCT") or row.get("fg3_pct")),
            _pct_out(row.get("FT_PCT") or row.get("ft_pct")),
            _safe_int(row.get("PLUS_MINUS") or row.get("plus_minus"), 0),
        )

    return boxscore_by_game, gamelogs_by_player, kept_rows, scanned_rows


def _build_player_rows_eoin(player_stats_csv, game_meta, team_id_to_abbr):
    boxscore_by_game = defaultdict(list)
    gamelogs_by_player = defaultdict(list)
    kept_rows = 0
    scanned_rows = 0

    for row in _iter_csv(player_stats_csv):
        scanned_rows += 1
        game_id = _normalize_game_id(row.get("gameId") or row.get("GAME_ID"))
        meta = game_meta.get(game_id)
        if not meta:
            continue

        pid = _safe_int(row.get("personId") or row.get("PLAYER_ID"), 0)
        if not pid:
            continue

        is_home = _to_bool(row.get("home"))
        team_id = meta["homeTeamId"] if is_home else meta["awayTeamId"]
        if not team_id:
            continue

        mins_raw = str(row.get("numMinutes") or row.get("MIN") or "").strip()
        mins = _parse_min(mins_raw)
        if mins <= 0:
            continue

        win_val = row.get("win")
        wl_hint = "W" if _to_bool(win_val) else ("L" if str(win_val or "").strip() else None)

        reb_def = _safe_int(row.get("reboundsDefensive"), 0)
        reb_off = _safe_int(row.get("reboundsOffensive"), 0)
        reb_total = _safe_int(row.get("reboundsTotal"), reb_def + reb_off)

        kept_rows = _add_box_and_log_row(
            kept_rows,
            boxscore_by_game,
            gamelogs_by_player,
            team_id_to_abbr,
            meta,
            game_id,
            pid,
            team_id,
            mins_raw if mins_raw else f"{_safe_round(mins, 1)}",
            mins,
            _safe_int(row.get("points"), 0),
            reb_total,
            _safe_int(row.get("assists"), 0),
            _safe_int(row.get("steals"), 0),
            _safe_int(row.get("blocks"), 0),
            _safe_int(row.get("turnovers"), 0),
            _safe_int(row.get("threePointersMade"), 0),
            _safe_int(row.get("threePointersAttempted"), 0),
            _safe_int(row.get("fieldGoalsMade"), 0),
            _safe_int(row.get("fieldGoalsAttempted"), 0),
            _safe_int(row.get("freeThrowsMade"), 0),
            _safe_int(row.get("freeThrowsAttempted"), 0),
            _pct_out(row.get("fieldGoalsPercentage")),
            _pct_out(row.get("threePointersPercentage")),
            _pct_out(row.get("freeThrowsPercentage")),
            _safe_int(row.get("plusMinusPoints"), 0),
            wl_hint=wl_hint,
            is_home_hint=is_home,
        )

    return boxscore_by_game, gamelogs_by_player, kept_rows, scanned_rows


def build_index(input_dir, output_path):
    t0 = time.time()
    schema = _detect_schema(input_dir)
    if not schema:
        print(
            "ERROR: unsupported input schema. Expected either:\n"
            "  legacy: games.csv + games_details.csv + teams.csv\n"
            "  eoin:   Games.csv + PlayerStatistics.csv + TeamHistories.csv",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Loading CSVs from {input_dir} ...", flush=True)
    print(f"Detected schema: {schema}", flush=True)

    if schema == "legacy":
        games_path = os.path.join(input_dir, "games.csv")
        details_path = os.path.join(input_dir, "games_details.csv")
        teams_path = os.path.join(input_dir, "teams.csv")
        team_id_to_abbr = _load_team_map_legacy(teams_path)
        player_positions = {}
        games_by_date, game_meta, seasons_set, game_rows = _build_games_legacy(games_path, team_id_to_abbr)
        boxscore_by_game, gamelogs_by_player, player_rows_kept, player_rows_scanned = _build_player_rows_legacy(
            details_path,
            game_meta,
            team_id_to_abbr,
        )
    else:
        games_path = os.path.join(input_dir, "Games.csv")
        details_path = os.path.join(input_dir, "PlayerStatistics.csv")
        teams_path = os.path.join(input_dir, "TeamHistories.csv")
        players_path = os.path.join(input_dir, "Players.csv")
        team_id_to_abbr = _load_team_map_eoin(teams_path)
        player_positions = _load_player_positions(players_path)
        games_by_date, game_meta, seasons_set, game_rows = _build_games_eoin(games_path, team_id_to_abbr)
        boxscore_by_game, gamelogs_by_player, player_rows_kept, player_rows_scanned = _build_player_rows_eoin(
            details_path,
            game_meta,
            team_id_to_abbr,
        )

    print(
        f"  games_rows={game_rows:,}  games_indexed={len(game_meta):,}  "
        f"player_rows_scanned={player_rows_scanned:,}  player_rows_kept={player_rows_kept:,}",
        flush=True,
    )

    print("Sorting player logs ...", flush=True)
    for pid in gamelogs_by_player:
        gamelogs_by_player[pid].sort(key=lambda g: g["_date_str"])

    seasons_covered = sorted(seasons_set)
    max_date = max(games_by_date.keys()) if games_by_date else ""
    min_date = min(games_by_date.keys()) if games_by_date else ""

    print("Computing sample rolling/hit-rate payloads ...", flush=True)
    # Keep behavior parity with existing downstream expectations:
    # rolling/hit-rates are computed on demand in LocalNBAStats,
    # but we validate fields here on one sample player.
    if gamelogs_by_player:
        sample_pid = next(iter(gamelogs_by_player.keys()))
        sample_logs = list(reversed(gamelogs_by_player[sample_pid]))[:25]
        _compute_rolling(sample_logs)
        _compute_hit_rates(sample_logs)

    index = {
        "schema": schema,
        "games_by_date": dict(games_by_date),
        "boxscore_by_game": dict(boxscore_by_game),
        "gamelogs_by_player": dict(gamelogs_by_player),
        "team_id_to_abbr": team_id_to_abbr,
        "player_positions": player_positions,
        "seasons_covered": seasons_covered,
        "min_date": min_date,
        "max_date": max_date,
    }

    print(f"Saving index to {output_path} ...", flush=True)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as fh:
        pickle.dump(index, fh, protocol=pickle.HIGHEST_PROTOCOL)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    elapsed = time.time() - t0
    print(
        f"\nIndexed {len(game_meta):,} games and {player_rows_kept:,} player rows "
        f"-> index.pkl ({size_mb:.0f} MB) in {elapsed:.0f}s",
        flush=True,
    )
    if seasons_covered:
        print(f"Seasons covered: {seasons_covered[0]} - {seasons_covered[-1]}")
    if min_date and max_date:
        print(f"Date range: {min_date} - {max_date}")


if __name__ == "__main__":
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _default_eoin_dir = os.path.join(_repo_root, "NBA Database (1947 - Present)")
    _default_legacy_dir = os.path.join(_repo_root, "data", "reference", "kaggle_nba")
    _default_input = _default_eoin_dir if os.path.isdir(_default_eoin_dir) else _default_legacy_dir

    parser = argparse.ArgumentParser(description="Build local NBA backtest index from CSV data")
    parser.add_argument(
        "--input-dir",
        default=_default_input,
        help=(
            "Input directory. Supported schemas:\n"
            " legacy: games.csv + games_details.csv + teams.csv\n"
            " eoin:   Games.csv + PlayerStatistics.csv + TeamHistories.csv"
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output pickle path (default: <input-dir>/index.pkl)",
    )
    args = parser.parse_args()

    input_dir = args.input_dir
    output_path = args.output or os.path.join(input_dir, "index.pkl")
    build_index(input_dir, output_path)
