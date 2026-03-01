#!/usr/bin/env python3
"""
Derive closing lines from the snapshot history.

For each (event_id, book, market, player_name) combination, the closing line
is the last snapshot recorded at or before the event's commence_time.

Run after backfill_odds_history.py has populated the snapshots table.

Usage
-----
.venv/Scripts/python.exe scripts/build_closing_lines.py
.venv/Scripts/python.exe scripts/build_closing_lines.py --date-from 2026-02-01 --date-to 2026-02-25
.venv/Scripts/python.exe scripts/build_closing_lines.py --db /path/to/custom.sqlite
"""

import argparse
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.nba_odds_store import OddsStore


def build_closing_lines(store, date_from=None, date_to=None):
    """
    Scan the snapshots table and derive closing lines.

    Closing line = last snapshot recorded at ts_utc <= commence_time
    for each (event_id, book, market, player_name) tuple.

    Returns (saved_count, total_derived_count).
    """
    # Use UTC-6 offset so "Feb 10" game commence_times (which are stored as
    # 2026-02-11T00:xx:xxZ UTC) resolve to their correct NBA calendar date.
    clauses, vals = [], []
    if date_from:
        clauses.append("date(datetime(substr(commence_time,1,19), '-6 hours')) >= ?")
        vals.append(date_from)
    if date_to:
        clauses.append("date(datetime(substr(commence_time,1,19), '-6 hours')) <= ?")
        vals.append(date_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = (
        f"SELECT event_id, book, market, player_name, "
        f"       side, line, odds, ts_utc, commence_time "
        f"FROM snapshots {where} "
        f"ORDER BY event_id, book, market, player_name, ts_utc"
    )
    cur = store._conn.execute(sql, vals)
    rows = cur.fetchall()

    # For each key, track the latest valid over/under snapshot
    # (ts_utc <= commence_time)
    groups = defaultdict(lambda: {"over": None, "under": None})

    for event_id, book, market, player_name, side, line, odds, ts_utc, commence_time in rows:
        if not commence_time:
            continue
        # Only use snapshots taken before game started
        if ts_utc > commence_time:
            continue
        key = (event_id, book, market, player_name)
        current = groups[key][side]
        if current is None or ts_utc > current["ts_utc"]:
            groups[key][side] = {
                "ts_utc":        ts_utc,
                "line":          line,
                "odds":          odds,
                "commence_time": commence_time,
            }

    # Build closing_line rows
    closing_rows = []
    for (event_id, book, market, player_name), sides in groups.items():
        over_d  = sides.get("over")
        under_d = sides.get("under")
        if not over_d and not under_d:
            continue
        primary   = over_d or under_d
        close_ts  = primary["ts_utc"]
        close_line = primary["line"]
        ct        = primary["commence_time"]
        closing_rows.append({
            "event_id":         event_id,
            "book":             book,
            "market":           market,
            "player_name":      player_name,
            "close_ts_utc":     close_ts,
            "close_line":       close_line,
            "close_over_odds":  over_d["odds"]  if over_d  else None,
            "close_under_odds": under_d["odds"] if under_d else None,
            "commence_time":    ct,
        })

    saved = store.upsert_closing_lines(closing_rows) if closing_rows else 0
    return saved, len(closing_rows)


def main():
    p = argparse.ArgumentParser(description="Derive closing lines from snapshot history")
    p.add_argument("--date-from", default=None, help="Only process events from this date")
    p.add_argument("--date-to",   default=None, help="Only process events up to this date")
    p.add_argument("--db",        default=None, help="Override SQLite DB path")
    args = p.parse_args()

    store = OddsStore(db_path=args.db)
    saved, total = build_closing_lines(store, args.date_from, args.date_to)
    store.close()

    result = {
        "success":  True,
        "derived":  total,
        "saved":    saved,
        "dateFrom": args.date_from,
        "dateTo":   args.date_to,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
