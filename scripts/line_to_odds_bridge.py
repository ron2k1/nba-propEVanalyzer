#!/usr/bin/env python3
"""
Bridge LineStore JSONL snapshots into OddsStore SQLite for zero-cost closing-line coverage.

LineStore (data/line_history/*.jsonl) holds live snapshots from collect_lines.
OddsStore (data/reference/odds_history/odds_history.sqlite) holds snapshots + closing_lines.
This bridge copies pre-game LineStore snapshots into OddsStore so build_closing_lines.py
can derive closes without additional historical API cost.

Schema mapping:
  LineStore: game_id, player_name, stat, line, over_odds, under_odds, book,
             timestamp_utc, commence_time, home_team_abbr, away_team_abbr
  OddsStore: event_id, player_name, market (=STAT_TO_MARKET[stat]), line, odds,
             ts_utc, book, side (over|under), home_team, away_team, commence_time, source

Idempotency: Uses INSERT OR IGNORE; exact duplicates (ts, event, book, market, player, side, line, odds)
             are skipped. Safe to run repeatedly.

Usage
-----
.venv/Scripts/python.exe scripts/line_to_odds_bridge.py 2026-02-20 2026-02-25
.venv/Scripts/python.exe scripts/line_to_odds_bridge.py 2026-02-27 --dry-run
.venv/Scripts/python.exe scripts/line_to_odds_bridge.py --books betmgm,draftkings --stats pts,reb,ast
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.nba_line_store import LineStore, LINE_HISTORY_DIR
from core.nba_odds_store import OddsStore, STAT_TO_MARKET

# Book key normalization: LineStore may use display names (e.g. "DraftKings") -> Odds API key
_BOOK_NORMALIZE = {
    "draftkings": "draftkings",
    "fanduel":    "fanduel",
    "betmgm":     "betmgm",
    "betmgmsportsbook": "betmgm",
    "betmgm sportsbook": "betmgm",
    "caesars":    "caesars",
    "pinnacle":   "pinnacle",
    "pointsbet":  "pointsbet",
    "wynnbet":    "wynnbet",
    "barstool":   "barstool",
    "betrivers":  "betrivers",
}


def _book_key(raw: str) -> str:
    raw_norm = str(raw or "").strip().lower()
    k = raw_norm.replace(" ", "")
    return _BOOK_NORMALIZE.get(raw_norm, _BOOK_NORMALIZE.get(k, k))


def _abbr_to_full_name(abbr: str) -> str:
    """Convert NBA abbr (e.g. MIN) to full team name for OddsStore find_event_for_game."""
    if not abbr or len(str(abbr).strip()) < 2:
        return ""
    try:
        from nba_api.stats.static import teams as nba_teams_static
        all_teams = {t["abbreviation"].upper(): t for t in nba_teams_static.get_teams()}
        t = all_teams.get(str(abbr).strip().upper())
        return (t.get("full_name") or "") if t else ""
    except Exception:
        return ""


def _iter_dates(date_from: str, date_to: str):
    cur = datetime.strptime(date_from[:10], "%Y-%m-%d").date()
    end = datetime.strptime(date_to[:10], "%Y-%m-%d").date()
    while cur <= end:
        yield cur.isoformat()
        cur += timedelta(days=1)


def _line_snapshot_to_odds_rows(snap: dict, stat_filter: set | None, book_filter: set | None) -> list[dict]:
    """
    Convert one LineStore snapshot into OddsStore snapshot rows (one per side: over, under).

    Returns list of rows for upsert_snapshots, or empty if filtered out.
    """
    stat = str(snap.get("stat") or "").strip().lower()
    if not stat or stat not in STAT_TO_MARKET:
        return []
    if stat_filter is not None and stat not in stat_filter:
        return []

    book_raw = snap.get("book") or ""
    book_key = _book_key(book_raw)
    if book_filter is not None and book_key not in book_filter:
        return []

    event_id = snap.get("game_id") or ""
    player_name = snap.get("player_name") or ""
    line = snap.get("line")
    ts_utc = snap.get("timestamp_utc") or ""
    commence_time = snap.get("commence_time") or ""
    home_abbr = snap.get("home_team_abbr") or ""
    away_abbr = snap.get("away_team_abbr") or ""

    if not event_id or not player_name or line is None:
        return []
    try:
        line = float(line)
    except (TypeError, ValueError):
        return []

    home_team = _abbr_to_full_name(home_abbr) or ""
    away_team = _abbr_to_full_name(away_abbr) or ""

    market = STAT_TO_MARKET[stat]
    rows = []
    over_odds = snap.get("over_odds")
    under_odds = snap.get("under_odds")
    if over_odds is not None:
        try:
            rows.append({
                "ts_utc":        ts_utc,
                "event_id":      event_id,
                "book":          book_key,
                "market":        market,
                "player_name":   player_name,
                "side":          "over",
                "line":          line,
                "odds":          int(over_odds),
                "home_team":     home_team,
                "away_team":     away_team,
                "commence_time": commence_time,
                "source":        "line_store_bridge",
            })
        except (TypeError, ValueError):
            pass
    if under_odds is not None:
        try:
            rows.append({
                "ts_utc":        ts_utc,
                "event_id":      event_id,
                "book":          book_key,
                "market":        market,
                "player_name":   player_name,
                "side":          "under",
                "line":          line,
                "odds":          int(under_odds),
                "home_team":     home_team,
                "away_team":     away_team,
                "commence_time": commence_time,
                "source":        "line_store_bridge",
            })
        except (TypeError, ValueError):
            pass
    return rows


def run_bridge(
    date_from: str,
    date_to: str | None = None,
    line_history_dir: str | None = None,
    odds_db_path: str | None = None,
    books: str | None = None,
    stats: str | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Bridge LineStore snapshots into OddsStore for [date_from, date_to].
    Returns result dict with inserted, skipped, errors, etc.
    """
    date_to = date_to or date_from
    line_store = LineStore(data_dir=line_history_dir or LINE_HISTORY_DIR)
    odds_store = OddsStore(db_path=odds_db_path)

    book_filter = None
    if books:
        book_filter = {_book_key(b.strip()) for b in books.split(",") if b.strip()}
    stat_filter = None
    if stats:
        stat_filter = {s.strip().lower() for s in stats.split(",") if s.strip()}
        stat_filter = {s for s in stat_filter if s in STAT_TO_MARKET}

    total_read = 0
    total_rows = 0
    inserted = 0
    errors = []

    try:
        for date_str in _iter_dates(date_from, date_to):
            snaps = line_store.get_snapshots(date_str)
            total_read += len(snaps)
            rows = []
            for snap in snaps:
                try:
                    rows.extend(_line_snapshot_to_odds_rows(snap, stat_filter, book_filter))
                except Exception as e:
                    errors.append({"date": date_str, "snap": str(snap)[:100], "error": str(e)})
                    if len(errors) <= 20:  # cap error list
                        pass
                    else:
                        continue

            if not dry_run and rows:
                inserted += odds_store.upsert_snapshots(rows)
            total_rows += len(rows)

        return {
            "success":       True,
            "dateFrom":      date_from,
            "dateTo":        date_to,
            "snapshotsRead": total_read,
            "rowsConverted": total_rows,
            "rowsInserted":  inserted,
            "dryRun":        dry_run,
            "errors":        errors[:50],
            "errorCount":    len(errors),
        }
    finally:
        odds_store.close()


def main():
    p = argparse.ArgumentParser(
        description="Bridge LineStore JSONL → OddsStore SQLite for closing-line coverage"
    )
    p.add_argument(
        "date_from",
        nargs="?",
        default=None,
        help="Start date YYYY-MM-DD (default: today)",
    )
    p.add_argument(
        "date_to",
        nargs="?",
        default=None,
        help="End date YYYY-MM-DD (default: same as date_from)",
    )
    p.add_argument("--dry-run", action="store_true", help="Convert but do not write")
    p.add_argument("--books", default=None, help="Comma-separated books to include")
    p.add_argument("--stats", default=None, help="Comma-separated stats (e.g. pts,reb,ast)")
    p.add_argument("--line-dir", default=None, help="Override LineStore directory")
    p.add_argument("--db", default=None, help="Override OddsStore SQLite path")
    args = p.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_from = args.date_from or today
    date_to = args.date_to or date_from

    try:
        result = run_bridge(
            date_from=date_from,
            date_to=date_to,
            line_history_dir=args.line_dir,
            odds_db_path=args.db,
            books=args.books,
            stats=args.stats,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("success") else 1)
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
