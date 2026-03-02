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
import sqlite3
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
        return len(params)

    def get_closing_line(self, event_id, market, player_name, book=None):
        """
        Return closing line dict for one player prop, or None if not found.
        Keys: book, close_line, close_over_odds, close_under_odds, close_ts_utc.

        Tries exact player name match first, then last-name partial match.
        If book is None, returns first available book (alphabetical).
        """
        def _query(name_clause, name_val):
            if book:
                return self._conn.execute(
                    f"SELECT book,close_line,close_over_odds,close_under_odds,close_ts_utc "
                    f"FROM closing_lines "
                    f"WHERE event_id=? AND market=? AND {name_clause} AND book=? LIMIT 1",
                    (event_id, market, name_val, book),
                ).fetchone()
            return self._conn.execute(
                f"SELECT book,close_line,close_over_odds,close_under_odds,close_ts_utc "
                f"FROM closing_lines "
                f"WHERE event_id=? AND market=? AND {name_clause} "
                f"ORDER BY book LIMIT 1",
                (event_id, market, name_val),
            ).fetchone()

        # 1. Exact match
        row = _query("player_name=?", player_name)
        # 2. Last-name partial match
        if not row and player_name:
            last = player_name.strip().split()[-1]
            if len(last) > 2:
                row = _query("player_name LIKE ?", f"%{last}%")
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
        book_clause = "AND book=?" if book else ""
        book_param  = [book] if book else []

        def _run(name_clause, name_val):
            return self._conn.execute(
                f"SELECT book,close_line,close_over_odds,close_under_odds,close_ts_utc "
                f"FROM closing_lines "
                f"WHERE market=? AND {name_clause} "
                f"AND date(datetime(substr(commence_time,1,19), '-6 hours'))=? "
                f"{book_clause} "
                f"ORDER BY book LIMIT 1",
                [market, name_val, date_str] + book_param,
            ).fetchone()

        row = _run("player_name=?", player_name)
        if not row and player_name:
            last = player_name.strip().split()[-1]
            if len(last) > 2:
                row = _run("player_name LIKE ?", f"%{last}%")
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

        Uses the same fuzzy player-name matching as get_closing_line().
        If book is None, returns the alphabetically first book at the earliest ts.

        Returns dict: {book, open_line, open_over_odds, open_under_odds, open_ts_utc}
        or None if no snapshot found.
        """
        def _query(name_clause, name_val):
            bc     = "AND book=?" if book else ""
            extra  = [book] if book else []

            # Step 1: find the earliest ts_utc for this prop
            min_row = self._conn.execute(
                f"SELECT MIN(ts_utc) FROM snapshots "
                f"WHERE event_id=? AND market=? AND player_name {name_clause} {bc}",
                [event_id, market, name_val] + extra,
            ).fetchone()
            if not min_row or not min_row[0]:
                return None
            min_ts = min_row[0]

            # Step 2: fetch over + under at that timestamp
            rows = self._conn.execute(
                f"SELECT side, line, odds, book FROM snapshots "
                f"WHERE event_id=? AND market=? AND player_name {name_clause} "
                f"AND ts_utc=? {bc} ORDER BY book, side",
                [event_id, market, name_val, min_ts] + extra,
            ).fetchall()
            if not rows:
                return None

            over_line = over_odds = under_odds = snap_book = None
            for side, line_val, odds_val, bk in rows:
                if side == "over" and over_line is None:
                    over_line, over_odds, snap_book = line_val, odds_val, bk
                elif side == "under" and under_odds is None:
                    under_odds = odds_val
            if over_line is None:
                return None
            return {
                "book":            snap_book,
                "open_line":       over_line,
                "open_over_odds":  over_odds,
                "open_under_odds": under_odds,
                "open_ts_utc":     min_ts,
            }

        row = _query("=?", player_name)
        if not row and player_name:
            last = player_name.strip().split()[-1]
            if len(last) > 2:
                row = _query("LIKE ?", f"%{last}%")
        return row

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
