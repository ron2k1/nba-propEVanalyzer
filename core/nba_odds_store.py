#!/usr/bin/env python3
"""
SQLite-backed store for historical Odds API snapshots and derived closing lines.

Tables
------
runs          - metadata for each backfill run (resumability)
snapshots     - raw odds captures (one row per ts x event x book x market x player x side)
closing_lines - derived from snapshots: last line per event x book x market x player
                recorded before the game's commence_time

Usage
-----
from core.nba_odds_store import OddsStore, STAT_TO_MARKET, MARKET_TO_STAT

store = OddsStore()
store.upsert_snapshots(rows)
line = store.get_closing_line(event_id="abc123", market="player_points",
                               player_name="Anthony Edwards")
"""

import os
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Stat key -> Odds API player prop market string
# ---------------------------------------------------------------------------

STAT_TO_MARKET = {
    "pts":  "player_points",
    "reb":  "player_rebounds",
    "ast":  "player_assists",
    "fg3m": "player_threes",
    "tov":  "player_turnovers",
    "pra":  "player_points_rebounds_assists",
    "stl":  "player_steals",
    "blk":  "player_blocks",
}

MARKET_TO_STAT = {v: k for k, v in STAT_TO_MARKET.items()}

# Partial team-name fragments used for fuzzy game matching (abbreviation -> unique fragment).
_ABBR_TO_NAME_PART = {
    "ATL": "hawks",       "BOS": "celtics",      "BKN": "nets",
    "CHA": "hornets",     "CHI": "bulls",         "CLE": "cavaliers",
    "DAL": "mavericks",   "DEN": "nuggets",       "DET": "pistons",
    "GSW": "warriors",    "HOU": "rockets",       "IND": "pacers",
    "LAC": "clippers",    "LAL": "lakers",        "MEM": "grizzlies",
    "MIA": "heat",        "MIL": "bucks",         "MIN": "timberwolves",
    "NOP": "pelicans",    "NYK": "knicks",        "OKC": "thunder",
    "ORL": "magic",       "PHI": "76ers",         "PHX": "suns",
    "POR": "blazers",     "SAC": "kings",         "SAS": "spurs",
    "TOR": "raptors",     "UTA": "jazz",          "WAS": "wizards",
}

# ---------------------------------------------------------------------------
# Player name normalization for fuzzy matching
# ---------------------------------------------------------------------------

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _norm_player_name(name):
    """Normalize player name: strip diacritics, periods, suffixes, collapse initials."""
    name = unicodedata.normalize("NFKD", str(name or ""))
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    name = re.sub(r"\s+", " ", name).strip()
    # Collapse adjacent single-letter tokens: "c j" -> "cj"
    toks = name.split()
    merged = []
    i = 0
    while i < len(toks):
        if len(toks[i]) == 1 and i + 1 < len(toks) and len(toks[i + 1]) == 1:
            run = toks[i]
            while i + 1 < len(toks) and len(toks[i + 1]) == 1:
                i += 1
                run += toks[i]
            merged.append(run)
        else:
            merged.append(toks[i])
        i += 1
    return " ".join(t for t in merged if t not in _SUFFIXES)


# Known name variants: NBA Stats name -> Odds API name (normalized)
_PLAYER_ALIASES = {
    _norm_player_name(k): _norm_player_name(v)
    for k, v in {
        "CJ McCollum": "C.J. McCollum",
        "RJ Barrett": "R.J. Barrett",
        "AJ Green": "A.J. Green",
        "GG Jackson": "G.G. Jackson",
        "PJ Washington": "P.J. Washington",
        "TJ McConnell": "T.J. McConnell",
        "OG Anunoby": "O.G. Anunoby",
        "KJ Martin": "K.J. Martin",
        "JT Thor": "J.T. Thor",
        "EJ Liddell": "E.J. Liddell",
        "AJ Johnson": "A.J. Johnson",
        "TJ Warren": "T.J. Warren",
        "DJ Carton": "D.J. Carton",
        "Nic Claxton": "Nicolas Claxton",
        "Moe Wagner": "Moritz Wagner",
        "Bub Carrington": "Carlton Carrington",
        "Ron Holland": "Ronald Holland",
        "Lu Dort": "Luguentz Dort",
        "Herb Jones": "Herbert Jones",
        "Cam Thomas": "Cameron Thomas",
        "Cam Johnson": "Cameron Johnson",
        "Cam Payne": "Cameron Payne",
        "Pat Connaughton": "Patrick Connaughton",
        "Svi Mykhailiuk": "Sviatoslav Mykhailiuk",
        "Ish Wainright": "Ishmail Wainright",
        "Mo Bamba": "Mohamed Bamba",
    }.items()
}
# Reverse direction too
_PLAYER_ALIASES.update({v: k for k, v in list(_PLAYER_ALIASES.items())})


def _resolve_player_name_candidate(query_name, candidate_names):
    """
    Resolve a requested player name to one stored in OddsStore.

    Matching order:
    1. Case-insensitive raw exact match
    2. Normalized exact match (punctuation/suffix/initial tolerant)
    3. Alias-normalized exact match
    4. Unique last-name fallback within the candidate set

    Returns the stored player_name string or None when ambiguous/unmatched.
    """
    query_raw = str(query_name or "").strip()
    if not query_raw:
        return None

    names = []
    seen = set()
    for name in candidate_names or []:
        raw = str(name or "").strip()
        if raw and raw not in seen:
            names.append(raw)
            seen.add(raw)
    if not names:
        return None

    query_raw_lower = query_raw.lower()
    for cand in names:
        if cand.lower() == query_raw_lower:
            return cand

    query_norm = _norm_player_name(query_raw)
    if not query_norm:
        return None

    norm_targets = {query_norm}
    alias_norm = _PLAYER_ALIASES.get(query_norm)
    if alias_norm:
        norm_targets.add(alias_norm)

    norm_rows = []
    for cand in names:
        cand_norm = _norm_player_name(cand)
        if cand_norm:
            norm_rows.append((cand, cand_norm))

    for target in norm_targets:
        for cand, cand_norm in norm_rows:
            if cand_norm == target:
                return cand

    last_targets = {
        norm.split()[-1]
        for norm in norm_targets
        if norm and norm.split() and len(norm.split()[-1]) > 2
    }
    if not last_targets:
        return None

    last_matches = {
        cand_norm: cand
        for cand, cand_norm in norm_rows
        if cand_norm.split() and cand_norm.split()[-1] in last_targets
    }
    if len(last_matches) == 1:
        return next(iter(last_matches.values()))
    return None


_DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "reference", "odds_history", "odds_history.sqlite",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    date_from   TEXT NOT NULL,
    date_to     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    error       TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    ts_utc        TEXT NOT NULL,
    sport         TEXT NOT NULL DEFAULT 'basketball_nba',
    event_id      TEXT NOT NULL,
    book          TEXT NOT NULL,
    market        TEXT NOT NULL,
    player_name   TEXT NOT NULL,
    side          TEXT NOT NULL,
    line          REAL NOT NULL,
    odds          INTEGER NOT NULL,
    home_team     TEXT,
    away_team     TEXT,
    commence_time TEXT,
    source        TEXT,
    UNIQUE (ts_utc, event_id, book, market, player_name, side, line, odds)
);

CREATE TABLE IF NOT EXISTS closing_lines (
    event_id         TEXT NOT NULL,
    book             TEXT NOT NULL,
    market           TEXT NOT NULL,
    player_name      TEXT NOT NULL,
    close_ts_utc     TEXT NOT NULL,
    close_line       REAL NOT NULL,
    close_over_odds  INTEGER,
    close_under_odds INTEGER,
    commence_time    TEXT,
    PRIMARY KEY (event_id, book, market, player_name)
);

CREATE INDEX IF NOT EXISTS idx_snap_event_market_player
    ON snapshots (event_id, market, player_name, book, ts_utc);
CREATE INDEX IF NOT EXISTS idx_snap_ts
    ON snapshots (ts_utc);
CREATE INDEX IF NOT EXISTS idx_snap_commence
    ON snapshots (commence_time, home_team, away_team);
CREATE INDEX IF NOT EXISTS idx_close_event_market
    ON closing_lines (event_id, market, player_name);
"""


class OddsStore:
    """SQLite-backed odds snapshot and closing line store."""

    def __init__(self, db_path=None):
        self._path = db_path or _DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._player_name_candidates_cache = {}
        self._init_db()

    def _init_db(self):
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Runs (resumability)
    # ------------------------------------------------------------------

    def start_run(self, date_from, date_to, run_id=None):
        rid = run_id or str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR REPLACE INTO runs "
            "(run_id, started_at, date_from, date_to, status) VALUES (?,?,?,?,'running')",
            (rid, now, date_from, date_to),
        )
        self._conn.commit()
        return rid

    def finish_run(self, run_id, status="done", error=None):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE runs SET finished_at=?, status=?, error=? WHERE run_id=?",
            (now, status, error, run_id),
        )
        self._conn.commit()

    def dates_with_snapshots(self):
        """Return sorted list of YYYY-MM-DD dates that have at least one snapshot."""
        cur = self._conn.execute(
            "SELECT DISTINCT substr(ts_utc,1,10) FROM snapshots ORDER BY 1"
        )
        return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def upsert_snapshots(self, rows):
        """
        Insert snapshot rows, ignoring exact duplicates.

        Required keys per row: ts_utc, event_id, book, market, player_name, side, line, odds
        Optional: sport, home_team, away_team, commence_time, source
        """
        sql = (
            "INSERT OR IGNORE INTO snapshots "
            "(ts_utc, sport, event_id, book, market, player_name, side, line, odds, "
            " home_team, away_team, commence_time, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        params = [
            (
                r["ts_utc"],
                r.get("sport", "basketball_nba"),
                r["event_id"],
                r["book"],
                r["market"],
                r["player_name"],
                r["side"],
                float(r["line"]),
                int(r["odds"]),
                r.get("home_team"),
                r.get("away_team"),
                r.get("commence_time"),
                r.get("source"),
            )
            for r in rows
        ]
        with self._conn:
            self._conn.executemany(sql, params)
        self._player_name_candidates_cache.clear()
        return len(params)

    def get_snapshots(self, date_str=None, event_id=None, market=None,
                      player_name=None, book=None, limit=2000):
        clauses, vals = [], []
        if date_str:
            clauses.append("substr(ts_utc,1,10)=?")
            vals.append(date_str)
        if event_id:
            clauses.append("event_id=?")
            vals.append(event_id)
        if market:
            clauses.append("market=?")
            vals.append(market)
        if player_name:
            clauses.append("player_name LIKE ?")
            vals.append(f"%{player_name}%")
        if book:
            clauses.append("book=?")
            vals.append(book)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.execute(
            f"SELECT ts_utc,sport,event_id,book,market,player_name,side,line,odds,"
            f"home_team,away_team,commence_time,source "
            f"FROM snapshots {where} ORDER BY ts_utc LIMIT ?",
            vals + [limit],
        )
        cols = ["ts_utc","sport","event_id","book","market","player_name",
                "side","line","odds","home_team","away_team","commence_time","source"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Closing lines
    # ------------------------------------------------------------------

    def upsert_closing_lines(self, rows):
        """
        Upsert (replace) closing line rows.

        Required keys: event_id, book, market, player_name, close_ts_utc,
                       close_line, close_over_odds, close_under_odds, commence_time
        """
        sql = (
            "INSERT OR REPLACE INTO closing_lines "
            "(event_id,book,market,player_name,close_ts_utc,close_line,"
            " close_over_odds,close_under_odds,commence_time) "
            "VALUES (?,?,?,?,?,?,?,?,?)"
        )
        params = [
            (
                r["event_id"], r["book"], r["market"], r["player_name"],
                r["close_ts_utc"], float(r["close_line"]),
                r.get("close_over_odds"), r.get("close_under_odds"),
                r.get("commence_time"),
            )
            for r in rows
        ]
        with self._conn:
            self._conn.executemany(sql, params)
        self._player_name_candidates_cache.clear()
        return len(params)

    def _candidate_player_names(self, table, market, event_id=None, date_str=None, book=None):
        cache_key = (table, str(event_id or ""), str(market or ""), str(date_str or ""), str(book or ""))
        cached = self._player_name_candidates_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        clauses = ["market=?"]
        vals = [market]
        if event_id is not None:
            clauses.append("event_id=?")
            vals.append(event_id)
        if date_str is not None:
            clauses.append("date(datetime(substr(commence_time,1,19), '-6 hours'))=?")
            vals.append(date_str)
        if book:
            clauses.append("book=?")
            vals.append(book)

        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT DISTINCT player_name FROM {table} WHERE {where} ORDER BY player_name",
            vals,
        ).fetchall()
        names = [row[0] for row in rows if row and row[0]]
        self._player_name_candidates_cache[cache_key] = tuple(names)
        return names

    def _resolve_stored_player_name(self, table, market, player_name, event_id=None, date_str=None, book=None):
        candidates = self._candidate_player_names(
            table,
            market,
            event_id=event_id,
            date_str=date_str,
            book=book,
        )
        return _resolve_player_name_candidate(player_name, candidates)

    def _resolve_player_name_for_scope(self, table, market, player_name, event_id=None, date_str=None, book=None):
        resolved_name = self._resolve_stored_player_name(
            table,
            market,
            player_name,
            event_id=event_id,
            date_str=date_str,
            book=book,
        )
        if not resolved_name:
            return None
        return resolved_name

    def _query_closing_line_row(self, market, player_name, event_id=None, date_str=None, book=None):
        clauses = ["market=?", "player_name=?"]
        vals = [market, player_name]
        if event_id is not None:
            clauses.insert(0, "event_id=?")
            vals.insert(0, event_id)
        if date_str is not None:
            clauses.append("date(datetime(substr(commence_time,1,19), '-6 hours'))=?")
            vals.append(date_str)
        if book:
            clauses.append("book=?")
            vals.append(book)

        where = " AND ".join(clauses)
        order_by = "close_ts_utc DESC, book" if date_str is not None else "book"
        return self._conn.execute(
            "SELECT book,close_line,close_over_odds,close_under_odds,close_ts_utc "
            f"FROM closing_lines WHERE {where} ORDER BY {order_by} LIMIT 1",
            vals,
        ).fetchone()

    def _query_snapshot_rows(self, event_id, market, player_name, book=None):
        clauses = ["event_id=?", "market=?", "player_name=?"]
        vals = [event_id, market, player_name]
        if book:
            clauses.append("book=?")
            vals.append(book)
        where = " AND ".join(clauses)
        rows = self._conn.execute(
            "SELECT ts_utc, book, side, line, odds, commence_time "
            f"FROM snapshots WHERE {where} ORDER BY ts_utc, book, side",
            vals,
        ).fetchall()
        return rows

    def _aggregate_snapshot_rows(self, rows):
        if not rows:
            return []

        from collections import OrderedDict

        grouped = OrderedDict()
        for ts, bk, side, line_val, odds_val, commence in rows:
            key = (ts, bk)
            if key not in grouped:
                grouped[key] = {
                    "ts_utc": ts,
                    "book": bk,
                    "line": None,
                    "over_odds": None,
                    "under_odds": None,
                    "commence_time": commence,
                }
            if side == "over":
                grouped[key]["line"] = line_val
                grouped[key]["over_odds"] = odds_val
            elif side == "under":
                grouped[key]["under_odds"] = odds_val
                if grouped[key]["line"] is None:
                    grouped[key]["line"] = line_val

        timeline = []
        for entry in grouped.values():
            minutes_to_tip = None
            if entry["commence_time"] and entry["ts_utc"]:
                try:
                    from datetime import datetime
                    ct = entry["commence_time"][:19].replace("T", " ")
                    ts = entry["ts_utc"][:19].replace("T", " ")
                    tip = datetime.strptime(ct, "%Y-%m-%d %H:%M:%S")
                    snap = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    minutes_to_tip = round((tip - snap).total_seconds() / 60, 1)
                except (ValueError, TypeError):
                    pass
            timeline.append({
                "ts_utc": entry["ts_utc"],
                "book": entry["book"],
                "line": entry["line"],
                "over_odds": entry["over_odds"],
                "under_odds": entry["under_odds"],
                "minutes_to_tip": minutes_to_tip,
            })
        return timeline

    def get_closing_line(self, event_id, market, player_name, book=None):
        """
        Return closing line dict for one player prop, or None if not found.
        Keys: book, close_line, close_over_odds, close_under_odds, close_ts_utc.

        Uses normalized exact matching plus a conservative unique last-name fallback.
        If book is None, returns first available book (alphabetical).
        """
        resolved_name = self._resolve_player_name_for_scope(
            "closing_lines", market, player_name, event_id=event_id, book=book
        )
        if not resolved_name:
            return None
        row = self._query_closing_line_row(
            market, resolved_name, event_id=event_id, book=book
        )
        if not row:
            return None
        return {
            "book":             row[0],
            "close_line":       row[1],
            "close_over_odds":  row[2],
            "close_under_odds": row[3],
            "close_ts_utc":     row[4],
        }

    def get_closing_line_by_player_date(self, player_name, market, date_str, book=None):
        """
        Fallback lookup: find closing line by player name + market + NBA date,
        without requiring a known event_id.  Used when find_event_for_game
        returns None (e.g. game snapshots missing but closing lines present).
        Returns the same dict shape as get_closing_line(), or None.
        """
        resolved_name = self._resolve_player_name_for_scope(
            "closing_lines", market, player_name, date_str=date_str, book=book
        )
        if not resolved_name:
            return None

        row = self._query_closing_line_row(
            market, resolved_name, date_str=date_str, book=book
        )
        if not row:
            return None
        return {
            "book":             row[0],
            "close_line":       row[1],
            "close_over_odds":  row[2],
            "close_under_odds": row[3],
            "close_ts_utc":     row[4],
        }

    def get_opening_line(self, event_id, market, player_name, book=None):
        """
        Return the EARLIEST snapshot (opening line proxy) for a player prop.

        Uses the same normalized matching as get_closing_line().
        If book is None, returns the alphabetically first book at the earliest ts.

        Returns dict: {book, open_line, open_over_odds, open_under_odds, open_ts_utc}
        or None if no snapshot found.
        """
        resolved_name = self._resolve_player_name_for_scope(
            "snapshots", market, player_name, event_id=event_id, book=book
        )
        if not resolved_name:
            return None

        rows = self._query_snapshot_rows(event_id, market, resolved_name, book=book)
        timeline = self._aggregate_snapshot_rows(rows)
        if not timeline:
            return None
        first = timeline[0]
        if first.get("line") is None:
            return None
        return {
            "book": first["book"],
            "open_line": first["line"],
            "open_over_odds": first["over_odds"],
            "open_under_odds": first["under_odds"],
            "open_ts_utc": first["ts_utc"],
        }

    def get_line_movement(self, event_id, market, player_name, book=None):
        """
        Return the full snapshot timeline for a player prop.

        Each entry represents one timestamp with aggregated over/under data.
        Returns list of dicts ordered by ts_utc (earliest first):
            [{ts_utc, book, line, over_odds, under_odds, minutes_to_tip}, ...]
        Empty list if no snapshots found.
        """
        resolved_name = self._resolve_player_name_for_scope(
            "snapshots", market, player_name, event_id=event_id, book=book
        )
        if not resolved_name:
            return []

        rows = self._query_snapshot_rows(event_id, market, resolved_name, book=book)
        return self._aggregate_snapshot_rows(rows)

    def get_line_movement_by_date(self, player_name, market, date_str, book=None):
        """
        Get line movement for a player prop by date (no event_id needed).

        Finds the event via commence_time date matching, then delegates to
        get_line_movement(). Returns list of timeline dicts or empty list.
        """
        extra = [book] if book else []
        bc = "AND book=?" if book else ""
        resolved_name = self._resolve_player_name_for_scope(
            "snapshots", market, player_name, date_str=date_str, book=book
        )
        if not resolved_name:
            return []

        row = self._conn.execute(
            f"SELECT DISTINCT event_id FROM snapshots "
            f"WHERE market=? AND player_name=? "
            f"AND date(datetime(substr(commence_time,1,19), '-6 hours'))=? {bc} "
            f"LIMIT 1",
            [market, resolved_name, date_str] + extra,
        ).fetchone()
        if not row:
            return []
        return self.get_line_movement(row[0], market, resolved_name, book=book)

    def get_closing_lines_for_date(self, date_str, market=None, book=None):
        """Return all closing lines for events that commenced on date_str (NBA local time)."""
        clauses = ["date(datetime(substr(commence_time,1,19), '-6 hours'))=?"]
        vals = [date_str]
        if market:
            clauses.append("market=?")
            vals.append(market)
        if book:
            clauses.append("book=?")
            vals.append(book)
        where = "WHERE " + " AND ".join(clauses)
        cur = self._conn.execute(
            f"SELECT event_id,book,market,player_name,close_ts_utc,"
            f"close_line,close_over_odds,close_under_odds,commence_time "
            f"FROM closing_lines {where} ORDER BY player_name",
            vals,
        )
        cols = ["event_id","book","market","player_name","close_ts_utc",
                "close_line","close_over_odds","close_under_odds","commence_time"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def find_event_for_game(self, home_abbr, away_abbr, date_str):
        """
        Find the event_id stored in the DB for a specific game on date_str.

        Matches using partial team-name fragments (e.g. MIN -> 'timberwolves').
        Returns event_id string or None.

        Note: NBA games tip off in the evening US time = early hours UTC the next
        day.  We use SQLite datetime arithmetic (UTC-6) to convert stored
        UTC commence_times back to their NBA calendar date before comparing.
        """
        home_frag = _ABBR_TO_NAME_PART.get(str(home_abbr).upper(), home_abbr).lower()
        away_frag = _ABBR_TO_NAME_PART.get(str(away_abbr).upper(), away_abbr).lower()
        cur = self._conn.execute(
            "SELECT DISTINCT event_id, home_team, away_team "
            "FROM snapshots "
            "WHERE date(datetime(substr(commence_time,1,19), '-6 hours'))=? LIMIT 200",
            (date_str,),
        )
        for event_id, home_team, away_team in cur.fetchall():
            ht = (home_team or "").lower()
            at = (away_team or "").lower()
            # Try both orderings — signal may store team as "home" when DB has them reversed
            if (home_frag in ht and away_frag in at) or (home_frag in at and away_frag in ht):
                return event_id
        return None

    # ------------------------------------------------------------------
    # Coverage summary
    # ------------------------------------------------------------------

    def coverage_summary(self):
        snap_count  = self._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        close_count = self._conn.execute("SELECT COUNT(*) FROM closing_lines").fetchone()[0]
        run_count   = self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        date_range  = self._conn.execute(
            "SELECT MIN(substr(ts_utc,1,10)), MAX(substr(ts_utc,1,10)) FROM snapshots"
        ).fetchone()
        events  = self._conn.execute(
            "SELECT COUNT(DISTINCT event_id) FROM snapshots"
        ).fetchone()[0]
        players = self._conn.execute(
            "SELECT COUNT(DISTINCT player_name) FROM closing_lines"
        ).fetchone()[0]
        books   = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT book FROM snapshots ORDER BY book"
        ).fetchall()]
        markets = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT market FROM snapshots ORDER BY market"
        ).fetchall()]
        return {
            "success":       True,
            "dbPath":        self._path,
            "snapshotCount": snap_count,
            "closingCount":  close_count,
            "runCount":      run_count,
            "eventCount":    events,
            "playerCount":   players,
            "dateFrom":      date_range[0],
            "dateTo":        date_range[1],
            "books":         books,
            "markets":       markets,
            "statKeys":      [MARKET_TO_STAT.get(m, m) for m in markets],
        }

    def coverage_by_date(self, date_from=None, date_to=None):
        """
        Per-date breakdown: closing rows (realLineSamples potential), event count.
        Uses NBA calendar date (commence_time -6h) for closing_lines.
        """
        clauses, vals = [], []
        if date_from:
            clauses.append(
                "date(datetime(substr(commence_time,1,19), '-6 hours')) >= ?"
            )
            vals.append(date_from)
        if date_to:
            clauses.append(
                "date(datetime(substr(commence_time,1,19), '-6 hours')) <= ?"
            )
            vals.append(date_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = (
            f"SELECT date(datetime(substr(commence_time,1,19), '-6 hours')) as nba_date, "
            f"       COUNT(DISTINCT event_id) as events, "
            f"       COUNT(*) as closing_rows "
            f"FROM closing_lines {where} "
            f"GROUP BY nba_date ORDER BY nba_date"
        )
        cur = self._conn.execute(sql, vals)
        by_date = [{"date": r[0], "events": r[1], "closingRows": r[2]} for r in cur.fetchall()]

        return {
            "success": True,
            "byDate": by_date,
            "totalClosingRows": sum(r["closingRows"] for r in by_date),
        }
