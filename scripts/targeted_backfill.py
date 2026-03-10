#!/usr/bin/env python3
"""
Targeted historical backfill: find dates with stale closing lines and re-backfill.

Queries OddsStore for all dates with closing lines, computes freshness
(minutes between last snapshot and tipoff), and runs backfill_odds_history.py
on dates where median freshness exceeds a threshold.

Usage
-----
.venv/Scripts/python.exe scripts/targeted_backfill.py --dry-run
.venv/Scripts/python.exe scripts/targeted_backfill.py --freshness-threshold 60 --max-requests 10000
.venv/Scripts/python.exe scripts/targeted_backfill.py --snap-offsets 10,30,60
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, UTC
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nba_odds_store import OddsStore
from scripts.backfill_odds_history import run_backfill
from scripts.build_closing_lines import build_closing_lines

_log = logging.getLogger("targeted_backfill")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)

DEFAULT_FRESHNESS_THRESHOLD = 60  # minutes
DEFAULT_SNAP_OFFSETS = "10,30,60"
DEFAULT_BOOKS = "betmgm,draftkings,fanduel,pinnacle"
DEFAULT_STATS = "pts,ast,reb,pra,fg3m,stl,blk"


def _compute_freshness(store: OddsStore) -> dict[str, dict]:
    """
    For each date with closing lines, compute freshness stats.

    Returns {date_str: {median_min, max_min, count, stale_count}}.
    """
    conn = store._conn
    rows = conn.execute("""
        SELECT
            date(datetime(substr(c.commence_time, 1, 19), '-6 hours')) AS game_date,
            c.commence_time,
            c.close_ts_utc
        FROM closing_lines c
        WHERE c.commence_time IS NOT NULL
          AND c.close_ts_utc IS NOT NULL
    """).fetchall()

    by_date: dict[str, list[float]] = defaultdict(list)

    for row in rows:
        game_date = row[0]
        try:
            commence = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
            close_ts = datetime.fromisoformat(row[2].replace("Z", "+00:00"))
            freshness_min = (commence - close_ts).total_seconds() / 60.0
            if freshness_min >= 0:  # only pre-game snapshots
                by_date[game_date].append(freshness_min)
        except (ValueError, TypeError):
            continue

    result = {}
    for date_str, mins in sorted(by_date.items()):
        result[date_str] = {
            "median_min": round(median(mins), 1) if mins else 999,
            "max_min": round(max(mins), 1) if mins else 999,
            "count": len(mins),
            "stale_count": sum(1 for m in mins if m > DEFAULT_FRESHNESS_THRESHOLD),
        }

    return result


def _find_stale_dates(
    freshness: dict[str, dict],
    threshold_min: float,
) -> list[str]:
    """Return sorted list of dates where median freshness exceeds threshold."""
    stale = []
    for date_str, stats in freshness.items():
        if stats["median_min"] > threshold_min:
            stale.append(date_str)
    return sorted(stale)


def main():
    parser = argparse.ArgumentParser(description="Targeted backfill for stale closing lines")
    parser.add_argument("--freshness-threshold", type=float, default=DEFAULT_FRESHNESS_THRESHOLD,
                        help=f"Median minutes-to-tip threshold to flag date as stale (default: {DEFAULT_FRESHNESS_THRESHOLD})")
    parser.add_argument("--snap-offsets", default=DEFAULT_SNAP_OFFSETS,
                        help=f"Comma-separated minutes-before-tip to snapshot (default: {DEFAULT_SNAP_OFFSETS})")
    parser.add_argument("--books", default=DEFAULT_BOOKS)
    parser.add_argument("--stats", default=DEFAULT_STATS)
    parser.add_argument("--max-requests", type=int, default=10000,
                        help="Hard cap on total API calls (default: 10000)")
    parser.add_argument("--db", default=None, help="Override SQLite DB path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print stale dates with freshness stats; no API calls")
    parser.add_argument("--workers", type=int, default=4,
                        help="Concurrent API workers for backfill (default: 4)")
    parser.add_argument("--date-from", default=None,
                        help="Only consider dates on or after this date (YYYY-MM-DD)")
    parser.add_argument("--date-to", default=None,
                        help="Only consider dates on or before this date (YYYY-MM-DD)")
    args = parser.parse_args()

    store = OddsStore(db_path=args.db)

    _log.info("Computing closing-line freshness...")
    freshness = _compute_freshness(store)
    _log.info("Found %d dates with closing lines", len(freshness))

    # Filter by date range if specified
    if args.date_from:
        freshness = {d: s for d, s in freshness.items() if d >= args.date_from}
    if args.date_to:
        freshness = {d: s for d, s in freshness.items() if d <= args.date_to}

    stale_dates = _find_stale_dates(freshness, args.freshness_threshold)

    if not stale_dates:
        _log.info("No stale dates found (threshold=%.0f min). All closing lines are fresh.",
                   args.freshness_threshold)
        result = {"success": True, "stale_dates": 0, "threshold_min": args.freshness_threshold}
        print(json.dumps(result))
        store.close()
        return 0

    _log.info("Found %d stale dates (median freshness > %.0f min):",
              len(stale_dates), args.freshness_threshold)
    for d in stale_dates:
        s = freshness[d]
        _log.info("  %s: median=%.0f min, max=%.0f min, closes=%d, stale=%d",
                   d, s["median_min"], s["max_min"], s["count"], s["stale_count"])

    if args.dry_run:
        result = {
            "success": True,
            "dry_run": True,
            "stale_dates": len(stale_dates),
            "threshold_min": args.freshness_threshold,
            "dates": {d: freshness[d] for d in stale_dates},
        }
        print(json.dumps(result, default=str))
        store.close()
        return 0

    # Run backfill on stale dates
    snap_offsets = [int(x.strip()) for x in args.snap_offsets.split(",") if x.strip()]
    stats_list = [s.strip() for s in args.stats.split(",") if s.strip()]
    workers = getattr(args, "workers", 4)
    total_api_calls = 0
    total_saved = 0
    backfill_results = []

    for date_str in stale_dates:
        if args.max_requests > 0 and total_api_calls >= args.max_requests:
            _log.warning("Budget cap reached: %d/%d calls. Stopping.", total_api_calls, args.max_requests)
            break

        remaining_budget = args.max_requests - total_api_calls if args.max_requests > 0 else 0
        _log.info("Backfilling %s (median freshness=%.0f min, budget remaining=%d)...",
                   date_str, freshness[date_str]["median_min"], remaining_budget)

        try:
            bf_result = run_backfill(
                date_from=date_str,
                date_to=date_str,
                books=args.books,
                stats=stats_list,
                snap_offsets=snap_offsets,
                max_requests=remaining_budget,
                resume=False,  # must re-fetch to get fresher snapshots
                workers=workers,
                sleep_sec=0.05,
            )
            calls = bf_result.get("requestCount", 0)
            saved = bf_result.get("totalSnapshots", 0)
            total_api_calls += calls
            total_saved += saved
            backfill_results.append({
                "date": date_str,
                "api_calls": calls,
                "saved": saved,
            })
            _log.info("  %s: %d API calls, %d rows saved", date_str, calls, saved)
        except Exception as e:
            _log.error("  %s: backfill failed: %s", date_str, e)
            backfill_results.append({"date": date_str, "error": str(e)})

    # Re-derive closing lines for backfilled dates
    if total_saved > 0:
        _log.info("Re-deriving closing lines for backfilled dates...")
        try:
            saved, derived = build_closing_lines(
                store,
                date_from=stale_dates[0],
                date_to=stale_dates[-1],
            )
            _log.info("Re-derived: %d total, %d saved", derived, saved)
        except Exception as e:
            _log.error("build_closing_lines failed: %s", e)

    store.close()

    result = {
        "success": True,
        "stale_dates": len(stale_dates),
        "threshold_min": args.freshness_threshold,
        "total_api_calls": total_api_calls,
        "total_saved": total_saved,
        "dates_backfilled": backfill_results,
    }
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
