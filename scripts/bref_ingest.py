#!/usr/bin/env python3
"""Ingest Basketball-Reference schedules + boxscores into local JSONL files."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
from datetime import date as _date
from datetime import datetime
from html.parser import HTMLParser

import requests
from nba_api.stats.static import players as nba_players_static
from nba_api.stats.static import teams as nba_teams_static


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CURATED_DIR = os.path.join(ROOT, "data", "bref", "curated")
DEFAULT_RAW_DIR = os.path.join(ROOT, "data", "bref", "raw")
BASE_BREF = "https://www.basketball-reference.com"

TEAM_NAME_ALIASES = {
    "LA Clippers": "Los Angeles Clippers",
    "New Jersey Nets": "Brooklyn Nets",
    "New Orleans Hornets": "New Orleans Pelicans",
    "New Orleans/Oklahoma City Hornets": "New Orleans Pelicans",
    "Charlotte Bobcats": "Charlotte Hornets",
    "Seattle SuperSonics": "Oklahoma City Thunder",
    "New York Knickerbockers": "New York Knicks",
    "San Francisco Warriors": "Golden State Warriors",
}

MONTH_NAMES = {
    1: "january",
    2: "february",
    3: "march",
    4: "april",
    5: "may",
    6: "june",
    7: "july",
    8: "august",
    9: "september",
    10: "october",
    11: "november",
    12: "december",
}

_ALL_TEAMS = nba_teams_static.get_teams()
_TEAM_BY_ABBR = {str(t.get("abbreviation", "") or "").upper(): t for t in _ALL_TEAMS}
_TABLE_RE = re.compile(
    r'(<table\b[^>]*\bid\s*=\s*["\'](?P<id>[^"\']+)["\'][^>]*>.*?</table>)',
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)


def _normalize_text(value):
    raw = str(value or "").strip().lower()
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^a-z0-9 ]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _strip_name_suffixes(normalized_name):
    if not normalized_name:
        return normalized_name
    suffixes = {"jr", "sr", "ii", "iii", "iv"}
    tokens = normalized_name.split()
    while tokens and tokens[-1].rstrip(".") in suffixes:
        tokens.pop()
    return " ".join(tokens).strip()


def _normalize_space(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def _parse_yyyy_mm_dd(value):
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _season_default_range(end_year):
    return _date(end_year - 1, 10, 1), _date(end_year, 6, 30)


def _month_key_for_day(day):
    season_end_year = day.year + 1 if day.month >= 10 else day.year
    return season_end_year, day.month


def _iter_month_keys(day_from, day_to):
    seen = set()
    cur = _date(day_from.year, day_from.month, 1)
    while cur <= day_to:
        key = _month_key_for_day(cur)
        if key not in seen:
            seen.add(key)
            yield key
        next_month = cur.month + 1
        next_year = cur.year
        if next_month > 12:
            next_month = 1
            next_year += 1
        cur = _date(next_year, next_month, 1)


def _month_schedule_url(season_end_year, month):
    month_name = MONTH_NAMES[int(month)]
    return f"{BASE_BREF}/leagues/NBA_{int(season_end_year)}_games-{month_name}.html"


def _safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_num(value, default=0.0):
    s = str(value or "").strip()
    if not s:
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


class _TableRowParser(HTMLParser):
    """Extract table rows keyed by `data-stat` from a single table fragment."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._in_row = False
        self._in_cell = False
        self._row = {}
        self._cell_stat = ""
        self._cell_text = []
        self._cell_href = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs or [])
        if tag == "tr":
            self._in_row = True
            self._row = {"_tr_class": _normalize_space(attrs.get("class", ""))}
            return
        if self._in_row and tag in {"td", "th"}:
            self._in_cell = True
            self._cell_stat = _normalize_space(attrs.get("data-stat", ""))
            self._cell_text = []
            self._cell_href = ""
            return
        if self._in_cell and tag == "a":
            href = _normalize_space(attrs.get("href", ""))
            if href and not self._cell_href:
                self._cell_href = href

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._in_cell:
            text = _normalize_space("".join(self._cell_text))
            if self._cell_stat:
                self._row[self._cell_stat] = text
                if self._cell_href:
                    self._row[f"{self._cell_stat}_href"] = self._cell_href
            self._in_cell = False
            self._cell_stat = ""
            self._cell_text = []
            self._cell_href = ""
            return
        if tag == "tr" and self._in_row:
            self.rows.append(self._row)
            self._in_row = False
            self._row = {}

    def handle_data(self, data):
        if self._in_cell:
            self._cell_text.append(data)


def _extract_tables(html_text):
    by_id = {}
    for m in _TABLE_RE.finditer(html_text or ""):
        table_id = _normalize_space(m.group("id"))
        table_html = m.group(1)
        if table_id and table_id not in by_id:
            by_id[table_id] = table_html

    for comment_body in _COMMENT_RE.findall(html_text or ""):
        for m in _TABLE_RE.finditer(comment_body):
            table_id = _normalize_space(m.group("id"))
            table_html = m.group(1)
            if table_id and table_id not in by_id:
                by_id[table_id] = table_html
    return by_id


def _parse_table_rows(table_html):
    parser = _TableRowParser()
    parser.feed(table_html or "")
    parser.close()
    return parser.rows


def _row_is_header(row):
    classes = str(row.get("_tr_class", "") or "").split()
    return "thead" in classes


def _parse_schedule_date(raw):
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in ("%a, %b %d, %Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _load_player_index():
    rows = nba_players_static.get_players()
    index = {}
    for p in rows:
        pid = _safe_int(p.get("id"), 0)
        name = str(p.get("full_name", "") or "")
        if pid <= 0 or not name:
            continue
        key = _normalize_text(name)
        if not key:
            continue
        index.setdefault(key, []).append(pid)
        short_key = _strip_name_suffixes(key)
        if short_key and short_key != key:
            index.setdefault(short_key, []).append(pid)
    for key in list(index.keys()):
        index[key] = sorted(set(index[key]), reverse=True)
    return index


def _resolve_player_id(player_name, player_index):
    key = _normalize_text(player_name)
    if not key:
        return 0
    ids = player_index.get(key)
    if ids:
        return int(ids[0])
    short_key = _strip_name_suffixes(key)
    ids = player_index.get(short_key)
    if ids:
        return int(ids[0])
    return 0


def _build_team_lookup():
    by_normalized = {}
    for team in _ALL_TEAMS:
        full_name = str(team.get("full_name", "") or "")
        if full_name:
            by_normalized[_normalize_text(full_name)] = team
        city = str(team.get("city", "") or "")
        nickname = str(team.get("nickname", "") or "")
        if city and nickname:
            by_normalized[_normalize_text(f"{city} {nickname}")] = team
            by_normalized[_normalize_text(nickname)] = team
    for alias, canonical in TEAM_NAME_ALIASES.items():
        canon_key = _normalize_text(canonical)
        team = by_normalized.get(canon_key)
        if team:
            by_normalized[_normalize_text(alias)] = team
    return by_normalized


def _resolve_team(team_name, lookup):
    key = _normalize_text(team_name)
    if not key:
        return None
    team = lookup.get(key)
    if not team:
        return None
    return {
        "teamId": _safe_int(team.get("id"), 0),
        "abbr": str(team.get("abbreviation", "") or "").upper(),
        "fullName": str(team.get("full_name", "") or ""),
    }


def _read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")


def _fetch_text(session, url, timeout=30, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            return resp.text
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def _parse_schedule_month(html_text, day_from, day_to, team_lookup):
    tables = _extract_tables(html_text)
    table_html = tables.get("schedule")
    if not table_html:
        return []

    games = []
    for row in _parse_table_rows(table_html):
        if _row_is_header(row):
            continue

        day = _parse_schedule_date(row.get("date_game"))
        if day is None or day < day_from or day > day_to:
            continue

        home_name = row.get("home_team_name", "")
        away_name = row.get("visitor_team_name", "")
        if not home_name or not away_name:
            continue

        home_team = _resolve_team(home_name, team_lookup)
        away_team = _resolve_team(away_name, team_lookup)
        if not home_team or not away_team:
            continue

        box_href = str(row.get("box_score_text_href", "") or "").strip()
        if not box_href:
            continue
        m = re.search(r"/boxscores/([^.]+)\.html", box_href)
        if not m:
            continue
        game_id = m.group(1)
        box_url = f"{BASE_BREF}{box_href}"

        games.append(
            {
                "date": day.isoformat(),
                "gameId": game_id,
                "homeTeamId": home_team["teamId"],
                "awayTeamId": away_team["teamId"],
                "homeAbbr": home_team["abbr"],
                "awayAbbr": away_team["abbr"],
                "homeTeamName": home_team["fullName"],
                "awayTeamName": away_team["fullName"],
                "boxscoreUrl": box_url,
                "source": "bref",
            }
        )
    return games


def _parse_boxscore_players(html_text, game, player_index):
    tables = _extract_tables(html_text)
    table_ids = sorted(tid for tid in tables if re.match(r"^box-[A-Z]{3}-game-basic$", tid))
    players = []
    for table_id in table_ids:
        team_abbr = table_id.split("-")[1].upper()
        if team_abbr == game["homeAbbr"]:
            team_id = game["homeTeamId"]
        elif team_abbr == game["awayAbbr"]:
            team_id = game["awayTeamId"]
        else:
            team = _TEAM_BY_ABBR.get(team_abbr)
            team_id = _safe_int((team or {}).get("id"), 0)

        for row in _parse_table_rows(tables[table_id]):
            if _row_is_header(row):
                continue
            player_name = str(row.get("player", "") or "").strip()
            if not player_name or player_name in {"Reserves", "Team Totals"}:
                continue
            mp = str(row.get("mp", "") or "").strip()
            if not mp:
                continue

            players.append(
                {
                    "gameId": game["gameId"],
                    "PLAYER_ID": _resolve_player_id(player_name, player_index),
                    "PLAYER_NAME": player_name,
                    "TEAM_ID": team_id,
                    "MIN": mp,
                    "PTS": _safe_num(row.get("pts")),
                    "REB": _safe_num(row.get("trb")),
                    "AST": _safe_num(row.get("ast")),
                    "STL": _safe_num(row.get("stl")),
                    "BLK": _safe_num(row.get("blk")),
                    "TOV": _safe_num(row.get("tov")),
                    "FG3M": _safe_num(row.get("fg3")),
                }
            )
    return players


def _merge_games(existing_games, new_games):
    by_id = {}
    for g in existing_games:
        gid = str(g.get("gameId", "") or "")
        if gid:
            by_id[gid] = g
    for g in new_games:
        gid = str(g.get("gameId", "") or "")
        if gid:
            by_id[gid] = g
    return sorted(by_id.values(), key=lambda x: (x.get("date", ""), x.get("gameId", "")))


def _merge_player_rows(existing_rows, new_rows):
    def key_fn(row):
        return (
            str(row.get("gameId", "") or ""),
            _safe_int(row.get("TEAM_ID"), 0),
            _safe_int(row.get("PLAYER_ID"), 0),
            _normalize_text(row.get("PLAYER_NAME", "")),
        )

    by_key = {}
    for row in existing_rows:
        k = key_fn(row)
        if k[0]:
            by_key[k] = row
    for row in new_rows:
        k = key_fn(row)
        if k[0]:
            by_key[k] = row
    return sorted(
        by_key.values(),
        key=lambda x: (
            x.get("gameId", ""),
            _safe_int(x.get("TEAM_ID"), 0),
            _normalize_text(x.get("PLAYER_NAME", "")),
        ),
    )


def main():
    ap = argparse.ArgumentParser(
        description="Download Basketball-Reference schedule + boxscores into local JSONL files."
    )
    ap.add_argument("--date-from", dest="date_from", help="Start date YYYY-MM-DD")
    ap.add_argument("--date-to", dest="date_to", help="End date YYYY-MM-DD")
    ap.add_argument(
        "--season",
        type=int,
        help="NBA season ending year (e.g., 2026 means 2025-10-01 through 2026-06-30).",
    )
    ap.add_argument("--sleep", type=float, default=0.15, help="Sleep between boxscore requests.")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds.")
    ap.add_argument("--max-games", type=int, default=0, help="Optional cap for quick test runs.")
    ap.add_argument(
        "--curated-dir",
        default=DEFAULT_CURATED_DIR,
        help=f"Curated output directory (default: {DEFAULT_CURATED_DIR})",
    )
    ap.add_argument(
        "--raw-dir",
        default=DEFAULT_RAW_DIR,
        help=f"Raw output directory (default: {DEFAULT_RAW_DIR})",
    )
    ap.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite curated files for this run instead of merging with existing rows.",
    )
    ap.add_argument(
        "--fail-on-empty",
        action="store_true",
        help="Exit with error if this run fetches zero games or zero player rows.",
    )
    args = ap.parse_args()

    if args.season and (args.date_from or args.date_to):
        raise SystemExit("Use either --season or --date-from/--date-to, not both.")

    if args.season:
        day_from, day_to = _season_default_range(args.season)
    else:
        day_from = _parse_yyyy_mm_dd(args.date_from)
        if day_from is None:
            raise SystemExit("Missing/invalid --date-from (YYYY-MM-DD).")
        day_to = _parse_yyyy_mm_dd(args.date_to) if args.date_to else day_from
        if day_to is None:
            raise SystemExit("Invalid --date-to (YYYY-MM-DD).")

    if day_to < day_from:
        raise SystemExit("--date-to must be >= --date-from")

    os.makedirs(args.curated_dir, exist_ok=True)
    os.makedirs(args.raw_dir, exist_ok=True)

    games_path = os.path.join(args.curated_dir, "games.jsonl")
    players_path = os.path.join(args.curated_dir, "player_boxscores.jsonl")

    team_lookup = _build_team_lookup()
    player_index = _load_player_index()
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; NBAPropAnalyzer/1.0)"})

    print(
        f"[bref_ingest] range={day_from} -> {day_to}  "
        f"replace={bool(args.replace)}  max_games={int(args.max_games)}"
    )

    month_keys = list(_iter_month_keys(day_from, day_to))
    schedule_games = []
    for season_end_year, month in month_keys:
        url = _month_schedule_url(season_end_year, month)
        print(f"[schedule] {season_end_year}-{month:02d}  {url}")
        try:
            html = _fetch_text(session, url, timeout=args.timeout)
            month_games = _parse_schedule_month(html, day_from, day_to, team_lookup)
            schedule_games.extend(month_games)
            print(f"  -> games parsed: {len(month_games)}")
        except Exception as e:
            print(f"  -> failed: {e}")

    dedup_games = {}
    for g in schedule_games:
        gid = str(g.get("gameId", "") or "")
        if gid:
            dedup_games[gid] = g
    games = sorted(dedup_games.values(), key=lambda x: (x.get("date", ""), x.get("gameId", "")))

    if args.max_games and args.max_games > 0:
        games = games[: int(args.max_games)]

    print(f"[boxscores] games to fetch: {len(games)}")

    all_player_rows = []
    unresolved_player_names = set()
    for idx, game in enumerate(games, start=1):
        url = game.get("boxscoreUrl")
        if not url:
            continue
        print(f"  [{idx:>4}/{len(games)}] {game['gameId']} {game['awayAbbr']} @ {game['homeAbbr']}")
        try:
            html = _fetch_text(session, url, timeout=args.timeout)
            rows = _parse_boxscore_players(html, game, player_index)
            all_player_rows.extend(rows)
            for r in rows:
                if _safe_int(r.get("PLAYER_ID"), 0) <= 0:
                    unresolved_player_names.add(str(r.get("PLAYER_NAME", "") or ""))
            if args.sleep > 0:
                time.sleep(args.sleep)
        except Exception as e:
            print(f"    -> failed: {e}")

    raw_stamp = f"{day_from.isoformat()}_to_{day_to.isoformat()}"
    raw_schedule_path = os.path.join(args.raw_dir, f"schedule_{raw_stamp}.json")
    raw_rows_path = os.path.join(args.raw_dir, f"player_rows_{raw_stamp}.json")
    with open(raw_schedule_path, "w", encoding="utf-8") as fh:
        json.dump(games, fh, indent=2)
    with open(raw_rows_path, "w", encoding="utf-8") as fh:
        json.dump(all_player_rows, fh, indent=2)

    existing_games = [] if args.replace else _read_jsonl(games_path)
    existing_rows = [] if args.replace else _read_jsonl(players_path)

    merged_games = _merge_games(existing_games, games)
    merged_rows = _merge_player_rows(existing_rows, all_player_rows)

    _write_jsonl(games_path, merged_games)
    _write_jsonl(players_path, merged_rows)

    manifest = {
        "success": True,
        "dateFrom": day_from.isoformat(),
        "dateTo": day_to.isoformat(),
        "failOnEmpty": bool(args.fail_on_empty),
        "replaceMode": bool(args.replace),
        "monthPagesScanned": len(month_keys),
        "gamesFetchedThisRun": len(games),
        "playerRowsFetchedThisRun": len(all_player_rows),
        "gamesCuratedTotal": len(merged_games),
        "playerRowsCuratedTotal": len(merged_rows),
        "unresolvedPlayersCount": len(unresolved_player_names),
        "unresolvedPlayersSample": sorted([n for n in unresolved_player_names if n])[:25],
        "curated": {
            "gamesPath": games_path,
            "playersPath": players_path,
        },
        "raw": {
            "schedulePath": raw_schedule_path,
            "playerRowsPath": raw_rows_path,
        },
    }
    manifest_path = os.path.join(args.raw_dir, f"manifest_{raw_stamp}.json")
    manifest["raw"]["manifestPath"] = manifest_path

    if args.fail_on_empty and (
        int(manifest["gamesFetchedThisRun"]) <= 0
        or int(manifest["playerRowsFetchedThisRun"]) <= 0
    ):
        manifest["success"] = False
        manifest["error"] = (
            "No games/player rows ingested. "
            "Check date range, internet access, or Basketball-Reference availability."
        )
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        print(json.dumps(manifest, indent=2))
        raise SystemExit(2)

    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
