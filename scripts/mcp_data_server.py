#!/usr/bin/env python3
"""
Read-only MCP data server for NBA prop engine.

Provides structured access to SQLite databases and JSONL files via MCP tools.
No imports from core/ — all mappings duplicated inline.

Transport: stdio
Dependencies: mcp (already installed), sqlite3, json, asyncio, pathlib
"""

import asyncio
import json
import os
import re
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

ODDS_DB = REPO_ROOT / "data" / "reference" / "odds_history" / "odds_history.sqlite"
JOURNAL_DB = REPO_ROOT / "data" / "decision_journal" / "decision_journal.sqlite"
LEAN_BETS_JSONL = REPO_ROOT / "data" / "lean_bets.jsonl"
PROP_JOURNAL_JSONL = REPO_ROOT / "data" / "prop_journal.jsonl"
LINE_HISTORY_DIR = REPO_ROOT / "data" / "line_history"

MAX_ROWS = 50
MAX_LIMIT_CAP = 200

# Stat shorthand -> Odds API market name (duplicated from core/nba_odds_store.py)
STAT_TO_MARKET = {
    "pts": "player_points",
    "reb": "player_rebounds",
    "ast": "player_assists",
    "fg3m": "player_threes",
    "tov": "player_turnovers",
    "pra": "player_points_rebounds_assists",
    "stl": "player_steals",
    "blk": "player_blocks",
}

# Whitelist of directories for read_file
FILE_TYPE_DIRS = {
    "calibration": "models",
    "backtest": "data/backtest_results",
    "backtest_60d": "data",
}

SAFE_FILENAME_RE = re.compile(r"^[\w.-]+$")


def _sanitize_fts(query: str) -> str:
    """Wrap each token in double-quotes so hyphens and special chars are literal."""
    tokens = query.split()
    return " ".join(f'"{t}"' for t in tokens if t)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return MAX_ROWS
    return max(1, min(int(limit), MAX_LIMIT_CAP))


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _build_query(base: str, conditions: list[str], params: list, order: str = "",
                 limit: int = MAX_ROWS) -> tuple[str, list]:
    sql = base
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    if order:
        sql += f" {order}"
    sql += " LIMIT ?"
    params.append(limit)
    return sql, params


def _format_results(rows: list[dict], total_count: int | None = None) -> str:
    result: dict = {"rows": rows, "count": len(rows)}
    if total_count is not None and total_count > len(rows):
        result["totalAvailable"] = total_count
        result["truncated"] = True
    return json.dumps(result, default=str)


def _rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _set_timeout(conn: sqlite3.Connection, seconds: int = 30):
    """Set a busy timeout. Read-only queries can't deadlock, but LIKE on 941K rows is slow."""
    conn.execute(f"PRAGMA busy_timeout = {seconds * 1000}")


# ---------------------------------------------------------------------------
# FTS5 Index (lazy, cached by mtime)
# ---------------------------------------------------------------------------

_fts_db_path: Path | None = None
_fts_mtimes: dict[str, float] = {}


def _source_mtimes() -> dict[str, float]:
    mtimes = {}
    for path in [LEAN_BETS_JSONL, PROP_JOURNAL_JSONL]:
        if path.exists():
            mtimes[str(path)] = path.stat().st_mtime
    if LINE_HISTORY_DIR.exists():
        for f in LINE_HISTORY_DIR.glob("*.jsonl"):
            mtimes[str(f)] = f.stat().st_mtime
    return mtimes


def _build_fts_index() -> Path:
    global _fts_db_path, _fts_mtimes

    current_mtimes = _source_mtimes()
    if _fts_db_path and _fts_db_path.exists() and current_mtimes == _fts_mtimes:
        return _fts_db_path

    # Create temp file
    fd, tmp_path = tempfile.mkstemp(suffix=".sqlite", prefix="nba_fts_")
    os.close(fd)
    tmp = Path(tmp_path)

    conn = sqlite3.connect(str(tmp))
    conn.execute("PRAGMA journal_mode = WAL")

    # -- lean_bets --
    conn.execute("""
        CREATE TABLE lean_bets (
            date TEXT, player_name TEXT, stat TEXT, line REAL, projection REAL,
            prob_over REAL, bin INTEGER, side TEXT, edge REAL, odds INTEGER,
            actual REAL, outcome TEXT, pnl REAL, used_real_line INTEGER,
            policy_pass INTEGER
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE lean_bets_fts USING fts5(
            player_name, stat, side, outcome, date,
            content='lean_bets', content_rowid='rowid', tokenize='unicode61'
        )
    """)
    if LEAN_BETS_JSONL.exists():
        batch = []
        with open(LEAN_BETS_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                batch.append((
                    r.get("date"), r.get("player_name"), r.get("stat"),
                    r.get("line"), r.get("projection"), r.get("prob_over"),
                    r.get("bin"), r.get("side"), r.get("edge"), r.get("odds"),
                    r.get("actual"), r.get("outcome"), r.get("pnl"),
                    1 if r.get("used_real_line") else 0,
                    1 if r.get("policy_pass") else 0,
                ))
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT INTO lean_bets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        batch)
                    conn.commit()
                    batch = []
        if batch:
            conn.executemany(
                "INSERT INTO lean_bets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch)
            conn.commit()
        conn.execute("INSERT INTO lean_bets_fts(lean_bets_fts) VALUES('rebuild')")
        conn.commit()

    # -- prop_journal --
    conn.execute("""
        CREATE TABLE prop_journal (
            pick_date TEXT, player_name TEXT, player_team_abbr TEXT,
            opponent_abbr TEXT, stat TEXT, line REAL, recommended_side TEXT,
            recommended_ev_pct REAL, result TEXT, pnl_1u REAL, actual_stat REAL,
            settled INTEGER, clv_line REAL, clv_odds_pct REAL
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE prop_journal_fts USING fts5(
            player_name, stat, player_team_abbr, opponent_abbr, result, pick_date,
            content='prop_journal', content_rowid='rowid', tokenize='unicode61'
        )
    """)
    if PROP_JOURNAL_JSONL.exists():
        batch = []
        with open(PROP_JOURNAL_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                batch.append((
                    r.get("pickDate"), r.get("playerName"), r.get("playerTeamAbbr"),
                    r.get("opponentAbbr"), r.get("stat"), r.get("line"),
                    r.get("recommendedSide"), r.get("recommendedEvPct"),
                    r.get("result"), r.get("pnl1u"), r.get("actualStat"),
                    1 if r.get("settled") else 0,
                    r.get("clvLine"), r.get("clvOddsPct"),
                ))
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT INTO prop_journal VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        batch)
                    conn.commit()
                    batch = []
        if batch:
            conn.executemany(
                "INSERT INTO prop_journal VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch)
            conn.commit()
        conn.execute("INSERT INTO prop_journal_fts(prop_journal_fts) VALUES('rebuild')")
        conn.commit()

    # -- line_history --
    conn.execute("""
        CREATE TABLE line_history (
            timestamp_utc TEXT, player_name TEXT, stat TEXT, line REAL,
            over_odds INTEGER, under_odds INTEGER, book TEXT,
            home_team_abbr TEXT, away_team_abbr TEXT, file_date TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE line_history_fts USING fts5(
            player_name, stat, book, home_team_abbr, away_team_abbr, file_date,
            content='line_history', content_rowid='rowid', tokenize='unicode61'
        )
    """)
    if LINE_HISTORY_DIR.exists():
        batch = []
        for jsonl_file in sorted(LINE_HISTORY_DIR.glob("*.jsonl")):
            file_date = jsonl_file.stem  # e.g. "2026-03-05"
            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    batch.append((
                        r.get("timestamp_utc"), r.get("player_name"),
                        r.get("stat"), r.get("line"),
                        r.get("over_odds"), r.get("under_odds"),
                        r.get("book"), r.get("home_team_abbr"),
                        r.get("away_team_abbr"), file_date,
                    ))
                    if len(batch) >= 5000:
                        conn.executemany(
                            "INSERT INTO line_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                            batch)
                        conn.commit()
                        batch = []
        if batch:
            conn.executemany(
                "INSERT INTO line_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                batch)
            conn.commit()
        conn.execute("INSERT INTO line_history_fts(line_history_fts) VALUES('rebuild')")
        conn.commit()

    conn.close()

    # Clean up old temp file
    if _fts_db_path and _fts_db_path.exists() and _fts_db_path != tmp:
        try:
            _fts_db_path.unlink()
        except OSError:
            pass

    _fts_db_path = tmp
    _fts_mtimes = current_mtimes
    return tmp


def _get_fts_conn() -> sqlite3.Connection:
    path = _build_fts_index()
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(server: FastMCP):
    conns = {}
    if ODDS_DB.exists():
        conns["odds"] = _open_readonly(ODDS_DB)
    if JOURNAL_DB.exists():
        conns["journal"] = _open_readonly(JOURNAL_DB)
    server._connections = conns  # type: ignore[attr-defined]
    try:
        yield
    finally:
        for c in conns.values():
            c.close()
        if _fts_db_path and _fts_db_path.exists():
            try:
                _fts_db_path.unlink()
            except OSError:
                pass


mcp = FastMCP("nba-data", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Tool 1: query_odds_snapshots
# ---------------------------------------------------------------------------

@mcp.tool(
    description="Query odds history snapshots (941K rows). Returns raw rows up to limit.",
)
def query_odds_snapshots(
    player_name: str = "",
    book: str = "",
    stat: str = "",
    date_from: str = "",
    date_to: str = "",
    side: str = "",
    limit: int = 50,
) -> str:
    conn: sqlite3.Connection = mcp._connections.get("odds")  # type: ignore[attr-defined]
    if not conn:
        return json.dumps({"error": "odds_history.sqlite not found"})

    _set_timeout(conn)
    lim = _clamp_limit(limit)
    conditions: list[str] = []
    params: list = []

    if player_name:
        conditions.append("player_name LIKE ? COLLATE NOCASE")
        params.append(f"%{player_name}%")
    if book:
        conditions.append("book = ? COLLATE NOCASE")
        params.append(book)
    if stat:
        market = STAT_TO_MARKET.get(stat.lower(), stat)
        conditions.append("market = ?")
        params.append(market)
    if date_from:
        conditions.append("ts_utc >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("ts_utc <= ?")
        params.append(date_to + "T23:59:59Z")
    if side:
        conditions.append("side = ? COLLATE NOCASE")
        params.append(side)

    sql, params = _build_query(
        "SELECT * FROM snapshots", conditions, params,
        order="ORDER BY ts_utc DESC", limit=lim,
    )
    try:
        cur = conn.execute(sql, params)
        rows = _rows_to_dicts(cur)
    except sqlite3.OperationalError as e:
        return json.dumps({"error": str(e)})

    return _format_results(rows)


# ---------------------------------------------------------------------------
# Tool 2: query_closing_lines
# ---------------------------------------------------------------------------

@mcp.tool(
    description="Query closing lines (175K rows). Returns raw rows up to limit.",
)
def query_closing_lines(
    player_name: str = "",
    book: str = "",
    stat: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
) -> str:
    conn: sqlite3.Connection = mcp._connections.get("odds")  # type: ignore[attr-defined]
    if not conn:
        return json.dumps({"error": "odds_history.sqlite not found"})

    _set_timeout(conn)
    lim = _clamp_limit(limit)
    conditions: list[str] = []
    params: list = []

    if player_name:
        conditions.append("player_name LIKE ? COLLATE NOCASE")
        params.append(f"%{player_name}%")
    if book:
        conditions.append("book = ? COLLATE NOCASE")
        params.append(book)
    if stat:
        market = STAT_TO_MARKET.get(stat.lower(), stat)
        conditions.append("market = ?")
        params.append(market)
    if date_from:
        conditions.append("commence_time >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("commence_time <= ?")
        params.append(date_to + "T23:59:59Z")

    sql, params = _build_query(
        "SELECT * FROM closing_lines", conditions, params,
        order="ORDER BY close_ts_utc DESC", limit=lim,
    )
    try:
        cur = conn.execute(sql, params)
        rows = _rows_to_dicts(cur)
    except sqlite3.OperationalError as e:
        return json.dumps({"error": str(e)})

    return _format_results(rows)


# ---------------------------------------------------------------------------
# Tool 3: query_journal
# ---------------------------------------------------------------------------

@mcp.tool(
    description="Query decision journal: signals LEFT JOIN outcomes. Returns signal + settlement + CLV data.",
)
def query_journal(
    player_name: str = "",
    stat: str = "",
    date_from: str = "",
    date_to: str = "",
    result: str = "",
    limit: int = 50,
) -> str:
    conn: sqlite3.Connection = mcp._connections.get("journal")  # type: ignore[attr-defined]
    if not conn:
        return json.dumps({"error": "decision_journal.sqlite not found"})

    _set_timeout(conn)
    lim = _clamp_limit(limit)
    conditions: list[str] = []
    params: list = []

    base = """
        SELECT s.*, o.game_id, o.settle_date, o.result, o.pnl_units,
               o.close_line, o.close_over_odds, o.close_under_odds,
               o.clv_delta, o.settled_at
        FROM signals s
        LEFT JOIN outcomes o ON s.signal_id = o.signal_id
    """

    if player_name:
        conditions.append("s.player_name LIKE ? COLLATE NOCASE")
        params.append(f"%{player_name}%")
    if stat:
        conditions.append("s.stat = ?")
        params.append(stat.lower())
    if date_from:
        conditions.append("s.ts_utc >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("s.ts_utc <= ?")
        params.append(date_to + "T23:59:59Z")
    if result:
        conditions.append("o.result = ?")
        params.append(result.lower())

    sql, params = _build_query(
        base, conditions, params,
        order="ORDER BY s.ts_utc DESC", limit=lim,
    )
    try:
        cur = conn.execute(sql, params)
        rows = _rows_to_dicts(cur)
    except sqlite3.OperationalError as e:
        return json.dumps({"error": str(e)})

    return _format_results(rows)


# ---------------------------------------------------------------------------
# Tool 4: search_bets
# ---------------------------------------------------------------------------

@mcp.tool(
    description="BM25-ranked full-text search across lean_bets and/or prop_journal JSONL data.",
)
def search_bets(
    query: str = "",
    source: str = "all",
    policy_pass_only: bool = False,
    limit: int = 50,
) -> str:
    if not query:
        return json.dumps({"error": "query is required"})

    safe_q = _sanitize_fts(query)
    conn = _get_fts_conn()
    lim = _clamp_limit(limit)
    results: dict = {}

    try:
        if source in ("all", "lean_bets"):
            sql = """
                SELECT lb.*, bm25(lean_bets_fts) AS rank
                FROM lean_bets_fts
                JOIN lean_bets lb ON lean_bets_fts.rowid = lb.rowid
                WHERE lean_bets_fts MATCH ?
            """
            params: list = [safe_q]
            if policy_pass_only:
                sql += " AND lb.policy_pass = 1"
            sql += " ORDER BY rank LIMIT ?"
            params.append(lim)
            cur = conn.execute(sql, params)
            results["lean_bets"] = _rows_to_dicts(cur)

        if source in ("all", "prop_journal"):
            sql = """
                SELECT pj.*, bm25(prop_journal_fts) AS rank
                FROM prop_journal_fts
                JOIN prop_journal pj ON prop_journal_fts.rowid = pj.rowid
                WHERE prop_journal_fts MATCH ?
            """
            params = [safe_q]
            sql += " ORDER BY rank LIMIT ?"
            params.append(lim)
            cur = conn.execute(sql, params)
            results["prop_journal"] = _rows_to_dicts(cur)

    except sqlite3.OperationalError as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()

    return json.dumps(results, default=str)


# ---------------------------------------------------------------------------
# Tool 5: search_lines
# ---------------------------------------------------------------------------

@mcp.tool(
    description="BM25-ranked full-text search across line_history JSONL data.",
)
def search_lines(
    query: str = "",
    date: str = "",
    book: str = "",
    limit: int = 50,
) -> str:
    if not query:
        return json.dumps({"error": "query is required"})

    safe_q = _sanitize_fts(query)
    conn = _get_fts_conn()
    lim = _clamp_limit(limit)

    try:
        sql = """
            SELECT lh.*, bm25(line_history_fts) AS rank
            FROM line_history_fts
            JOIN line_history lh ON line_history_fts.rowid = lh.rowid
            WHERE line_history_fts MATCH ?
        """
        params: list = [safe_q]
        if date:
            sql += " AND lh.file_date = ?"
            params.append(date)
        if book:
            sql += " AND lh.book = ? COLLATE NOCASE"
            params.append(book)
        sql += " ORDER BY rank LIMIT ?"
        params.append(lim)
        cur = conn.execute(sql, params)
        rows = _rows_to_dicts(cur)
    except sqlite3.OperationalError as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()

    return _format_results(rows)


# ---------------------------------------------------------------------------
# Tool 6: aggregate_performance
# ---------------------------------------------------------------------------

@mcp.tool(
    description="ROI/hit-rate summaries from lean_bets. Groups by stat, bin, book, date, or player.",
)
def aggregate_performance(
    group_by: str = "stat",
    stat: str = "",
    date_from: str = "",
    date_to: str = "",
    policy_pass_only: bool = True,
    real_line_only: bool = False,
) -> str:
    valid_groups = {"stat", "bin", "book", "date", "player"}
    if group_by not in valid_groups:
        return json.dumps({"error": f"group_by must be one of {valid_groups}"})

    conn = _get_fts_conn()

    group_col = "player_name" if group_by == "player" else group_by
    conditions: list[str] = []
    params: list = []

    if stat:
        conditions.append("stat = ?")
        params.append(stat.lower())
    if date_from:
        conditions.append("date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date <= ?")
        params.append(date_to)
    if policy_pass_only:
        conditions.append("policy_pass = 1")
    if real_line_only:
        conditions.append("used_real_line = 1")

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    sql = f"""
        SELECT {group_col} AS grp,
               COUNT(*) AS n,
               SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
               ROUND(100.0 * SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) / COUNT(*), 2) AS hit_pct,
               ROUND(SUM(pnl), 2) AS total_pnl,
               ROUND(100.0 * SUM(pnl) / COUNT(*), 2) AS roi_pct,
               ROUND(AVG(edge), 4) AS avg_edge
        FROM lean_bets
        {where}
        GROUP BY {group_col}
        ORDER BY n DESC
    """

    try:
        cur = conn.execute(sql, params)
        rows = _rows_to_dicts(cur)
    except sqlite3.OperationalError as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()

    return json.dumps({"rows": rows, "count": len(rows), "group_by": group_by}, default=str)


# ---------------------------------------------------------------------------
# Tool 7: coverage_report
# ---------------------------------------------------------------------------

@mcp.tool(
    description="Odds history coverage diagnostics: events/books/markets/snapshots per game_date.",
)
def coverage_report(
    date_from: str = "",
    date_to: str = "",
) -> str:
    conn: sqlite3.Connection = mcp._connections.get("odds")  # type: ignore[attr-defined]
    if not conn:
        return json.dumps({"error": "odds_history.sqlite not found"})

    _set_timeout(conn)
    conditions: list[str] = []
    params: list = []

    if date_from:
        conditions.append("DATE(commence_time) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(commence_time) <= ?")
        params.append(date_to)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    sql = f"""
        SELECT DATE(commence_time) AS game_date,
               COUNT(DISTINCT event_id) AS events,
               COUNT(DISTINCT book) AS books,
               COUNT(DISTINCT market) AS markets,
               COUNT(*) AS snapshots
        FROM snapshots
        {where}
        GROUP BY DATE(commence_time)
        ORDER BY game_date DESC
        LIMIT 60
    """

    try:
        cur = conn.execute(sql, params)
        daily = _rows_to_dicts(cur)

        # Recent runs
        run_cur = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 10"
        )
        runs = _rows_to_dicts(run_cur)
    except sqlite3.OperationalError as e:
        return json.dumps({"error": str(e)})

    totals_sql = f"SELECT COUNT(*) AS total_snapshots, COUNT(DISTINCT event_id) AS total_events FROM snapshots {where}"
    tot = conn.execute(totals_sql, list(params)).fetchone()
    totals = {"total_snapshots": tot[0], "total_events": tot[1]}

    return json.dumps({
        "totals": totals,
        "daily": daily,
        "recent_runs": runs,
    }, default=str)


# ---------------------------------------------------------------------------
# Tool 8: read_file
# ---------------------------------------------------------------------------

@mcp.tool(
    description="Return calibration, backtest, or backtest_60d JSON files raw. Whitelisted directories only.",
)
def read_file(
    file_type: str = "",
    filename: str = "",
) -> str:
    if file_type not in FILE_TYPE_DIRS:
        return json.dumps({"error": f"file_type must be one of {list(FILE_TYPE_DIRS.keys())}"})

    base_dir = REPO_ROOT / FILE_TYPE_DIRS[file_type]
    if not base_dir.exists():
        return json.dumps({"error": f"Directory not found: {FILE_TYPE_DIRS[file_type]}"})

    # If no filename, list available files
    if not filename:
        files = sorted(
            [f.name for f in base_dir.iterdir()
             if f.suffix == ".json" and f.is_file()],
            reverse=True,
        )[:50]
        return json.dumps({"files": files, "count": len(files), "directory": FILE_TYPE_DIRS[file_type]})

    # Validate filename
    if not SAFE_FILENAME_RE.match(filename):
        return json.dumps({"error": "Invalid filename. Alphanumeric, hyphens, underscores, dots only."})

    target = base_dir / filename
    if not target.exists():
        return json.dumps({"error": f"File not found: {filename}"})

    try:
        content = target.read_text(encoding="utf-8")
        # Try to parse as JSON for structured return
        data = json.loads(content)
        return json.dumps({"filename": filename, "data": data}, default=str)
    except json.JSONDecodeError:
        # Return raw text (truncated if huge)
        if len(content) > 100_000:
            return json.dumps({
                "filename": filename,
                "raw": content[:100_000],
                "truncated": True,
                "totalBytes": len(content),
            })
        return json.dumps({"filename": filename, "raw": content})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool 9: paper_summary
# ---------------------------------------------------------------------------

@mcp.tool(
    description="Aggregation over prop_journal JSONL for paper trading window. Returns signal count, hit rate, ROI, PnL, CLV+ rate.",
)
def paper_summary(
    window_days: int = 14,
) -> str:
    if not PROP_JOURNAL_JSONL.exists():
        return json.dumps({"error": "prop_journal.jsonl not found"})

    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=window_days)).strftime("%Y-%m-%d")

    signals = []
    with open(PROP_JOURNAL_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            pick_date = r.get("pickDate", "")
            if pick_date >= cutoff:
                signals.append(r)

    total = len(signals)
    settled = [s for s in signals if s.get("settled")]
    unsettled = total - len(settled)
    wins = sum(1 for s in settled if s.get("result") == "win")
    losses = sum(1 for s in settled if s.get("result") == "loss")
    pushes = sum(1 for s in settled if s.get("result") == "push")
    pnl = sum(s.get("pnl1u", 0) or 0 for s in settled)
    hit_rate = round(100.0 * wins / len(settled), 2) if settled else 0.0
    roi = round(100.0 * pnl / len(settled), 2) if settled else 0.0

    clv_positive = sum(
        1 for s in settled
        if (s.get("clvLine") or 0) > 0 and (s.get("clvOddsPct") or 0) > 0
    )
    clv_rate = round(100.0 * clv_positive / len(settled), 2) if settled else 0.0

    # Per-stat breakdown
    by_stat: dict = {}
    for s in settled:
        st = s.get("stat", "?")
        if st not in by_stat:
            by_stat[st] = {"n": 0, "wins": 0, "pnl": 0.0}
        by_stat[st]["n"] += 1
        if s.get("result") == "win":
            by_stat[st]["wins"] += 1
        by_stat[st]["pnl"] += s.get("pnl1u", 0) or 0

    for st, d in by_stat.items():
        d["hit_pct"] = round(100.0 * d["wins"] / d["n"], 2) if d["n"] else 0.0
        d["roi_pct"] = round(100.0 * d["pnl"] / d["n"], 2) if d["n"] else 0.0
        d["pnl"] = round(d["pnl"], 2)

    return json.dumps({
        "window_days": window_days,
        "cutoff": cutoff,
        "total_signals": total,
        "settled": len(settled),
        "unsettled": unsettled,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "hit_rate_pct": hit_rate,
        "roi_pct": roi,
        "pnl_units": round(pnl, 2),
        "clv_positive_pct": clv_rate,
        "by_stat": by_stat,
    }, default=str)


# ---------------------------------------------------------------------------
# Action Tools — 7 daily pipeline commands
# ---------------------------------------------------------------------------

_PYTHON = str(REPO_ROOT / ".venv" / "Scripts" / "python.exe")
_ENTRY = str(REPO_ROOT / "nba_mod.py")

PIPELINE_COMMANDS = {
    "collect_lines", "roster_sweep", "best_today", "top_picks",
    "paper_settle", "line_bridge", "odds_build_closes",
}


def _run_cli(argv: list[str], timeout: int = 600) -> str:
    """Run an nba_mod.py CLI command and return the last JSON line."""
    import subprocess
    cmd = [_PYTHON, _ENTRY] + argv
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        lines = result.stdout.strip().splitlines()
        if lines:
            return lines[-1]
        return json.dumps({"error": result.stderr.strip() or "no output"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Command timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# 1. collect_lines
@mcp.tool(
    description="Collect odds snapshots from sportsbooks. Runs collect_lines with specified books and stats.",
)
def collect_lines(
    books: str = "betmgm,draftkings,fanduel",
    stats: str = "pts,reb,ast,pra",
) -> str:
    return _run_cli(["collect_lines", "--books", books, "--stats", stats])


# 2. roster_sweep
@mcp.tool(
    description="Sweep all players in today's LineStore snapshots through the EV model. Logs qualifying signals + leans. Takes ~10 min.",
)
def roster_sweep(date: str = "") -> str:
    argv = ["roster_sweep"]
    if date:
        argv.append(date)
    return _run_cli(argv, timeout=900)


# 3. best_today
@mcp.tool(
    description="Show today's best policy-qualified picks from the decision journal.",
)
def best_today(limit: int = 20) -> str:
    return _run_cli(["best_today", str(limit)])


# 4. top_picks
@mcp.tool(
    description="Top N policy-qualified picks for today + best 2-leg parlay. Pre-tip final picks.",
)
def top_picks(limit: int = 5) -> str:
    return _run_cli(["top_picks", str(limit)])


# 5. line_bridge
@mcp.tool(
    description="Bridge collected lines into odds store for CLV tracking. Run after last game.",
)
def line_bridge(
    books: str = "betmgm,draftkings,fanduel",
    stats: str = "pts,reb,ast,pra",
) -> str:
    return _run_cli(["line_bridge", "--books", books, "--stats", stats])


# 6. odds_build_closes
@mcp.tool(
    description="Build closing lines from odds snapshots. Run end-of-day after line_bridge.",
)
def odds_build_closes() -> str:
    return _run_cli(["odds_build_closes"])


# 7. paper_settle
@mcp.tool(
    description="Settle paper trades, decision journal signals, and leans for a date. Run next morning.",
)
def paper_settle(date: str = "") -> str:
    if not date:
        return json.dumps({"error": "date required (YYYY-MM-DD)"})
    return _run_cli(["paper_settle", date])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
