#!/usr/bin/env python3
"""
Line snapshot collector.

Polls sportsbook prop lines for today's NBA games and appends them to
data/line_history/YYYY-MM-DD.jsonl with UTC timestamps.

Usage:
  .venv/Scripts/python.exe scripts/collect_lines.py                         # run once
  .venv/Scripts/python.exe scripts/collect_lines.py --interval 30           # poll every 30 min
  .venv/Scripts/python.exe scripts/collect_lines.py --books betmgm,draftkings
  .venv/Scripts/python.exe scripts/collect_lines.py --stats pts,reb,ast,fg3m,tov
  .venv/Scripts/python.exe scripts/collect_lines.py --stats all              # all 11 markets
  .venv/Scripts/python.exe scripts/collect_lines.py --stale                  # print stale lines after snapshot

Output:
  data/line_history/YYYY-MM-DD.jsonl   – one JSON object per line per book
  Prints a summary JSON to stdout on each poll.

API quota note:
  Cost = N_events × N_stats requests.  Default (5 stats, ~8 games) ≈ 40 requests/poll.
  At --interval 30 over a 12-hour window: ~960 requests/day.
  Use --interval 60 for lighter usage (~480 requests/day).
"""

import argparse
import json
import os
import sys
import time

# Ensure project root is on the path.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT, ".env"), override=True)

from core.nba_data_collection import get_todays_event_props_bulk
from core.nba_line_store import LineStore


def _collect_once(books: str, stats: list, store: LineStore, print_stale: bool) -> dict:
    result = get_todays_event_props_bulk(
        bookmakers=books,
        stats=stats,
    )

    if not result.get("success"):
        return {"success": False, "error": result.get("error"), "snapshotCount": 0}

    snapshots = result.get("snapshots", [])
    saved     = store.append_snapshots(snapshots)

    summary = {
        "success":       True,
        "timestamp":     result.get("timestamp"),
        "eventCount":    result.get("eventCount", 0),
        "snapshotCount": saved,
        "errors":        result.get("errors", []),
        "quota":         result.get("quota"),
    }

    if print_stale and saved > 0:
        date_str    = str(result.get("timestamp", ""))[:10]
        stale_lines = store.detect_stale_lines(date_str)
        summary["staleCount"] = len(stale_lines)
        summary["stale"]      = stale_lines[:20]  # top 20 by line_diff

    return summary


def main():
    parser = argparse.ArgumentParser(description="NBA prop line snapshot collector")
    parser.add_argument("--interval",  type=int,   default=0,
                        help="Polling interval in minutes (0 = run once)")
    parser.add_argument("--books",     type=str,   default="betmgm,draftkings,fanduel",
                        help="Comma-separated bookmaker keys")
    parser.add_argument("--stats",     type=str,   default="pts,reb,ast,fg3m,tov",
                        help="Comma-separated stat keys, or 'all'")
    parser.add_argument("--stale",     action="store_true",
                        help="Print stale-line report after each snapshot")
    parser.add_argument("--json",      action="store_true",
                        help="Output machine-readable JSON to stdout")
    args = parser.parse_args()

    stats = (
        ["all"] if args.stats.strip().lower() == "all"
        else [s.strip() for s in args.stats.split(",") if s.strip()]
    )
    store = LineStore()

    if args.interval <= 0:
        # Single run
        summary = _collect_once(args.books, stats, store, args.stale)
        if args.json:
            print(json.dumps(summary, default=str))
        else:
            _print_summary(summary)
        sys.exit(0 if summary.get("success") else 1)

    # Polling loop
    poll = 0
    while True:
        poll += 1
        print(f"[poll #{poll}] collecting lines...", flush=True)
        summary = _collect_once(args.books, stats, store, args.stale)
        if args.json:
            print(json.dumps(summary, default=str), flush=True)
        else:
            _print_summary(summary)

        if not summary.get("success"):
            print("  [warn] collection failed, will retry.", flush=True)

        sleep_sec = args.interval * 60
        print(f"  sleeping {args.interval}m...", flush=True)
        time.sleep(sleep_sec)


def _print_summary(s: dict) -> None:
    if not s.get("success"):
        print(f"  ERROR: {s.get('error')}", flush=True)
        return
    print(
        f"  ✓ {s['snapshotCount']} snapshots  "
        f"| {s['eventCount']} events  "
        f"| {s.get('timestamp','')[:19]}Z",
        flush=True,
    )
    errors = s.get("errors", [])
    if errors:
        print(f"  [warn] {len(errors)} fetch errors", flush=True)
    quota = s.get("quota") or {}
    if quota.get("remaining"):
        print(f"  API quota remaining: {quota['remaining']}", flush=True)
    stale = s.get("stale", [])
    if stale:
        print(f"\n  --- Stale lines ({s.get('staleCount',0)}) ---", flush=True)
        for item in stale[:10]:
            side_tag = "OVER" if item["recommended_side"] == "over" else "UNDER"
            print(
                f"  {item['player_name']:<30} {item['stat']:<5} "
                f"{side_tag} @ {item['stale_book']}  "
                f"line={item['stale_line']} vs consensus={item['consensus_line']}  "
                f"diff={item['line_diff']:+.1f}",
                flush=True,
            )


if __name__ == "__main__":
    main()
