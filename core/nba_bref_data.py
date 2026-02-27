#!/usr/bin/env python3
"""Local Basketball-Reference dataset access for backtests."""

from __future__ import annotations

import json
import os
from datetime import date as _date
from datetime import datetime

from nba_api.stats.static import teams as nba_teams_static

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BREF_CURATED_DIR = os.path.join(_ROOT, "data", "bref", "curated")
DEFAULT_BREF_GAMES_FILE = "games.jsonl"
DEFAULT_BREF_PLAYERS_FILE = "player_boxscores.jsonl"

_TEAMS_BY_ABBR = {
    str(t.get("abbreviation", "") or "").upper(): int(t.get("id", 0) or 0)
    for t in nba_teams_static.get_teams()
}


def _parse_date(value):
    if isinstance(value, _date):
        return value
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_stat_float(value):
    if value is None:
        return 0.0
    s = str(value).strip()
    if not s:
        return 0.0
    if s.endswith("%"):
        s = s[:-1]
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


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


class BrefLocalStore:
    """
    Local backtest data source built from Basketball-Reference.

    Expected files under `base_dir`:
      - games.jsonl
      - player_boxscores.jsonl
    """

    def __init__(self, base_dir=None):
        self.base_dir = base_dir or DEFAULT_BREF_CURATED_DIR
        self.games_path = os.path.join(self.base_dir, DEFAULT_BREF_GAMES_FILE)
        self.players_path = os.path.join(self.base_dir, DEFAULT_BREF_PLAYERS_FILE)
        self._loaded = False
        self._games_by_date = {}
        self._players_by_game = {}

    def _normalize_game_row(self, raw):
        day = _parse_date(raw.get("date"))
        if day is None:
            return None

        home_abbr = str(raw.get("homeAbbr", "") or "").upper()
        away_abbr = str(raw.get("awayAbbr", "") or "").upper()
        if not home_abbr or not away_abbr:
            return None

        game_id = str(raw.get("gameId", "") or "").strip()
        if not game_id:
            game_id = f"bref_{day.strftime('%Y%m%d')}_{away_abbr}_{home_abbr}"

        home_id = _safe_int(raw.get("homeTeamId"), 0) or _TEAMS_BY_ABBR.get(home_abbr, 0)
        away_id = _safe_int(raw.get("awayTeamId"), 0) or _TEAMS_BY_ABBR.get(away_abbr, 0)
        if home_id <= 0 or away_id <= 0:
            return None

        return {
            "date": day.isoformat(),
            "gameId": game_id,
            "homeTeamId": home_id,
            "awayTeamId": away_id,
            "homeAbbr": home_abbr,
            "awayAbbr": away_abbr,
            "homeTeamName": str(raw.get("homeTeamName", "") or ""),
            "awayTeamName": str(raw.get("awayTeamName", "") or ""),
            "source": str(raw.get("source", "bref") or "bref"),
            "boxscoreUrl": str(raw.get("boxscoreUrl", "") or ""),
        }

    def _normalize_player_row(self, raw):
        game_id = str(raw.get("gameId", "") or "").strip()
        if not game_id:
            return None

        team_id = _safe_int(raw.get("TEAM_ID"), 0)
        if team_id <= 0:
            return None

        min_raw = raw.get("MIN")
        # Keep "MM:SS" format as-is; backtest parser accepts this directly.
        min_str = "" if min_raw is None else str(min_raw).strip()
        if not min_str:
            min_str = "0:00"

        row = {
            "gameId": game_id,
            "PLAYER_ID": _safe_int(raw.get("PLAYER_ID"), 0),
            "PLAYER_NAME": str(raw.get("PLAYER_NAME", "") or ""),
            "TEAM_ID": team_id,
            "MIN": min_str,
            "PTS": _safe_stat_float(raw.get("PTS")),
            "REB": _safe_stat_float(raw.get("REB")),
            "AST": _safe_stat_float(raw.get("AST")),
            "STL": _safe_stat_float(raw.get("STL")),
            "BLK": _safe_stat_float(raw.get("BLK")),
            "TOV": _safe_stat_float(raw.get("TOV")),
            "FG3M": _safe_stat_float(raw.get("FG3M")),
        }
        return row

    def load(self):
        if self._loaded:
            return

        if not os.path.exists(self.games_path):
            raise FileNotFoundError(
                f"Missing BRef games file: {self.games_path}. "
                "Run scripts/bref_ingest.py first."
            )
        if not os.path.exists(self.players_path):
            raise FileNotFoundError(
                f"Missing BRef boxscore file: {self.players_path}. "
                "Run scripts/bref_ingest.py first."
            )

        games_by_date = {}
        for raw in _read_jsonl(self.games_path):
            game = self._normalize_game_row(raw)
            if not game:
                continue
            day = game["date"]
            games_by_date.setdefault(day, []).append(game)

        players_by_game = {}
        for raw in _read_jsonl(self.players_path):
            row = self._normalize_player_row(raw)
            if not row:
                continue
            players_by_game.setdefault(row["gameId"], []).append(row)

        # Deterministic order helps reproducibility and test snapshots.
        for day in list(games_by_date.keys()):
            games_by_date[day] = sorted(games_by_date[day], key=lambda g: g["gameId"])
        for game_id in list(players_by_game.keys()):
            players_by_game[game_id] = sorted(
                players_by_game[game_id],
                key=lambda r: (r["TEAM_ID"], str(r.get("PLAYER_NAME", "")), r["PLAYER_ID"]),
            )

        self._games_by_date = games_by_date
        self._players_by_game = players_by_game
        self._loaded = True

    def get_games_for_date(self, day):
        self.load()
        d = _parse_date(day)
        if d is None:
            return []
        return list(self._games_by_date.get(d.isoformat(), []))

    def get_boxscore_players(self, game_id):
        self.load()
        key = str(game_id or "").strip()
        if not key:
            return []
        return list(self._players_by_game.get(key, []))

    def teams_played_on_date(self, day):
        teams = set()
        for g in self.get_games_for_date(day):
            teams.add(int(g.get("homeTeamId", 0) or 0))
            teams.add(int(g.get("awayTeamId", 0) or 0))
        return teams

    def coverage_summary(self):
        self.load()
        game_count = sum(len(v) for v in self._games_by_date.values())
        day_count = len(self._games_by_date)
        player_rows = sum(len(v) for v in self._players_by_game.values())
        return {
            "baseDir": self.base_dir,
            "gamesFile": self.games_path,
            "playersFile": self.players_path,
            "days": day_count,
            "games": game_count,
            "playerRows": player_rows,
        }


def load_bref_store(base_dir=None):
    store = BrefLocalStore(base_dir=base_dir)
    store.load()
    return store
