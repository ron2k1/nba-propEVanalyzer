#!/usr/bin/env python3
"""
Historical Odds API backfill script.

For each date in [--date-from, --date-to]:
  1. Discover NBA events via the historical h2h endpoint (one API call per date).
  2. For each event x stat, fetch historical player prop odds at
     (commence_time - offset_minutes) to capture a near-closing snapshot.
  3. Upsert all snapshots into the OddsStore SQLite database.

Usage
-----
.venv/Scripts/python.exe scripts/backfill_odds_history.py ^
    --date-from 2026-02-01 --date-to 2026-02-25 ^
    --books betmgm,draftkings,fanduel ^
    --stats pts,reb,ast,fg3m,tov ^
    --offset-minutes 60 ^
    [--interval-minutes 0] ^
    [--max-requests 500] ^
    [--resume] ^
    [--dry-run]

Flags
-----
--offset-minutes   Minutes before game start to capture the closing snapshot (default 60).
--interval-minutes If >0, also take additional snapshots every N min before offset,
                   up to 2x offset. 0 = single snapshot per event (default, cheapest).
--max-requests     Hard cap on total API calls (0 = unlimited). Safety valve.
--resume           Skip dates that already have at least one snapshot in the DB.
--dry-run          Print what would be fetched; make no API calls and write nothing.
"""

import argparse
import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone, date as _date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"), override=True)

from core.nba_odds_store import OddsStore, STAT_TO_MARKET
from core.nba_data_collection import _odds_api_get

_STAT_DEFAULTS = ["pts", "reb", "ast", "fg3m", "tov", "stl", "blk"]
_BOOK_DEFAULTS = "betmgm,draftkings,fanduel"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(s):
    """Parse ISO-8601 UTC timestamp string into an aware datetime."""
    s = str(s or "").strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 2].split(".")[0], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {s!r}")


def _iter_dates(date_from_str, date_to_str):
    cur = datetime.strptime(date_from_str, "%Y-%m-%d").date()
    end = datetime.strptime(date_to_str, "%Y-%m-%d").date()
    while cur <= end:
        yield cur.isoformat()
        cur += timedelta(days=1)


def _historical_get(path, params, dry_run=False):
    """Call _odds_api_get with /historical/ prefix; returns the inner data list."""
    if dry_run:
        return {"success": True, "_dry_run": True, "events": [], "quota": {}, "timestamp": ""}
    resp = _odds_api_get(path, params=params, timeout=30)
    if not resp.get("success"):
        return resp
    # Historical endpoint wraps response: {"timestamp": "...", "data": [...]}
    outer = resp.get("data") or {}
    if isinstance(outer, dict):
        events = outer.get("data", [])
        ts = outer.get("timestamp", "")
    else:
        # Fallback: regular (non-historical) structure returns list directly
        events = outer if isinstance(outer, list) else []
        ts = ""
    return {
        "success":   True,
        "events":    events,
        "timestamp": ts,
        "quota":     resp.get("quota", {}),
    }


def _utc_to_nba_date(utc_str):
    """
    Convert a UTC tip-off timestamp to its NBA calendar date (local US time).

    NBA games tip off between roughly 1 PM and 10:30 PM US time.
    Subtracting 6 hours maps any tipoff UTC timestamp to the correct calendar
    date for the team's local timezone across all US time zones and both
    EST (UTC-5) and EDT (UTC-4) seasons.

    Examples:
      2026-02-11T00:30:00Z  (7:30 PM EST Feb 10)  -> 2026-02-10  ✓
      2026-02-11T03:30:00Z  (10:30 PM EST Feb 10) -> 2026-02-10  ✓
      2026-02-12T00:00:00Z  (7:00 PM EST Feb 11)  -> 2026-02-11  ✓
    """
    try:
        dt = datetime.strptime(str(utc_str)[:19], "%Y-%m-%dT%H:%M:%S")
        return (dt - timedelta(hours=6)).strftime("%Y-%m-%d")
    except Exception:
        return str(utc_str)[:10]


def _discover_events_for_date(date_str, sport, books, dry_run=False):
    """
    Use the historical h2h endpoint to find NBA events that commence on date_str.
    Fetches a snapshot at 18:00 UTC (1 PM ET — before any tip-off).

    Returns (list_of_event_dicts, error_string_or_None).
    Each event dict: {event_id, home_team, away_team, commence_time}.
    """
    ts = f"{date_str}T18:00:00Z"
    resp = _historical_get(
        f"historical/sports/{sport}/odds",
        params={"date": ts, "regions": "us", "markets": "h2h", "bookmakers": books},
        dry_run=dry_run,
    )
    if not resp.get("success"):
        return [], resp.get("error", "unknown error")

    events = []
    for ev in (resp.get("events") or []):
        ct = ev.get("commence_time", "")
        if not ct:
            continue
        # Filter to events whose local US tip-off date matches date_str.
        # NBA games start 7–10:30 PM ET which is 00:00–04:30 UTC the next day,
        # so we compare local date (UTC-6 proxy) instead of raw UTC date.
        if _utc_to_nba_date(ct) != date_str:
            continue
        events.append({
            "event_id":     ev["id"],
            "home_team":    ev.get("home_team", ""),
            "away_team":    ev.get("away_team", ""),
            "commence_time": ct,
        })
    return events, None


def _snapshot_timestamps(commence_time_str, offset_min, interval_min, snap_offsets=None):
    """
    Return list of UTC timestamp strings at which to fetch snapshots.

    If snap_offsets is a non-empty list of ints, each int is a minutes-before-tip
    offset and one snapshot is fetched per entry.  This is the preferred mode for
    multi-snapshot / true-closing-line runs, e.g. snap_offsets=[10, 60, 120, 240].

    Otherwise falls back to offset_min / interval_min behaviour:
    - Always includes commence_time - offset_min.
    - If interval_min > 0: also goes backward every interval_min minutes up to
      commence_time - 2*offset_min.
    """
    try:
        ct = _parse_iso(commence_time_str)
    except ValueError:
        return []

    if snap_offsets:
        return [
            (ct - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for m in snap_offsets
        ]

    if interval_min <= 0:
        t = ct - timedelta(minutes=offset_min)
        return [t.strftime("%Y-%m-%dT%H:%M:%SZ")]

    tss = []
    t = ct - timedelta(minutes=offset_min)
    max_lookback = ct - timedelta(minutes=offset_min * 2)
    while t >= max_lookback:
        tss.append(t.strftime("%Y-%m-%dT%H:%M:%SZ"))
        t -= timedelta(minutes=interval_min)
    return tss


def _fetch_prop_snapshot(event, stat, books, snapshot_ts, sport, dry_run=False):
    """
    Fetch historical player prop odds for one event x stat at snapshot_ts.
    Returns (list_of_snapshot_row_dicts, quota_dict).
    """
    market = STAT_TO_MARKET.get(stat)
    if not market:
        return [], {}

    resp = _historical_get(
        f"historical/sports/{sport}/events/{event['event_id']}/odds",
        params={
            "date":       snapshot_ts,
            "regions":    "us",
            "markets":    market,
            "bookmakers": books,
            "oddsFormat": "american",
        },
        dry_run=dry_run,
    )
    quota = resp.get("quota", {})
    if not resp.get("success"):
        return [], quota

    # The event-specific historical endpoint returns a single event dict (not a list).
    # The h2h discovery endpoint returns a list.  Normalise to always work with a list.
    raw_events = resp.get("events")
    if isinstance(raw_events, dict):
        event_list = [raw_events]         # single event -> wrap in list
    elif isinstance(raw_events, list):
        event_list = raw_events
    else:
        event_list = []

    actual_ts = resp.get("timestamp") or snapshot_ts

    rows = []
    for ev in event_list:
        for bm in (ev.get("bookmakers") or []):
            book_key = bm.get("key", "")
            for mkt in (bm.get("markets") or []):
                if mkt.get("key") != market:
                    continue
                # Group outcomes by player description (over/under pairs)
                by_player = {}
                for outcome in (mkt.get("outcomes") or []):
                    desc  = outcome.get("description", "") or ""
                    side  = outcome.get("name", "").lower()   # "Over" / "Under"
                    price = outcome.get("price")
                    point = outcome.get("point")
                    if not desc or price is None or point is None:
                        continue
                    by_player.setdefault(desc, {})[side] = {
                        "odds": int(price), "line": float(point)
                    }
                for player_name, sides in by_player.items():
                    over_d  = sides.get("over",  {})
                    under_d = sides.get("under", {})
                    line = (over_d.get("line") or under_d.get("line"))
                    if line is None:
                        continue
                    common = {
                        "ts_utc":        actual_ts,
                        "sport":         sport,
                        "event_id":      event["event_id"],
                        "book":          book_key,
                        "market":        market,
                        "player_name":   player_name,
                        "line":          line,
                        "home_team":     event["home_team"],
                        "away_team":     event["away_team"],
                        "commence_time": event["commence_time"],
                        "source":        "historical_api",
                    }
                    if over_d:
                        rows.append({**common, "side": "over",  "odds": over_d["odds"]})
                    if under_d:
                        rows.append({**common, "side": "under", "odds": under_d["odds"]})
    return rows, quota


# ---------------------------------------------------------------------------
# Main backfill function
# ---------------------------------------------------------------------------

def run_backfill(
    date_from,
    date_to,
    books=_BOOK_DEFAULTS,
    stats=None,
    offset_minutes=60,
    interval_minutes=0,
    snap_offsets=None,
    sport="basketball_nba",
    max_requests=0,
    resume=False,
    dry_run=False,
    db_path=None,
    workers=1,
    sleep_sec=0.05,
):
    """
    Run the historical odds backfill for [date_from, date_to].
    Returns a result dict with success, requestCount, totalSnapshots, errors.

    snap_offsets: list of ints — explicit minutes-before-tip offsets, e.g. [10, 60, 120, 240].
        When provided, overrides offset_minutes/interval_minutes.
        Each offset = 1 API call per (event × stat). Use to capture:
          10  = true closing line
          60  = standard pregame (1 hr before tip)
          120 = 2 hr before tip
          240 = 4 hr before tip (near-open for NBA props)
    """
    stats = stats or _STAT_DEFAULTS
    store = OddsStore(db_path=db_path)
    run_id = store.start_run(date_from, date_to)

    snap_desc = (
        f"snap_offsets={snap_offsets}" if snap_offsets
        else f"offset={offset_minutes}min  interval={interval_minutes}min"
    )
    print(
        f"[backfill] run_id={run_id}  {date_from} -> {date_to}  "
        f"books={books}  stats={','.join(stats)}  "
        f"{snap_desc}  dry_run={dry_run}",
        flush=True,
    )

    existing_dates = set(store.dates_with_snapshots()) if resume else set()
    request_count = 0
    total_snaps   = 0
    errors        = []
    quota_remaining = None

    try:
        for date_str in _iter_dates(date_from, date_to):
            if resume and date_str in existing_dates:
                print(f"  [{date_str}] SKIP (already in DB)", flush=True)
                continue

            if max_requests > 0 and request_count >= max_requests:
                print(f"  STOPPING: max_requests={max_requests} reached", flush=True)
                break

            # ---- Step 1: discover events for this date ----
            events, err = _discover_events_for_date(date_str, sport, books, dry_run=dry_run)
            request_count += 1
            if err:
                print(f"  [{date_str}] ERROR discovering events: {err}", flush=True)
                errors.append({"date": date_str, "stage": "discover", "error": err})
                continue
            if not events:
                print(f"  [{date_str}] no events found", flush=True)
                continue

            print(f"  [{date_str}] {len(events)} events", flush=True)

            # ---- Step 2: fetch props per event x stat ----
            day_snaps = 0

            # Build work items for this date
            work_items = []
            for event in events:
                tss = _snapshot_timestamps(event["commence_time"], offset_minutes, interval_minutes, snap_offsets)
                for ts_str in tss:
                    for stat in stats:
                        work_items.append((event, stat, ts_str))

            if workers > 1 and not dry_run and len(work_items) > 1:
                # Concurrent execution with bounded workers
                _lock = threading.Lock()

                def _fetch_one(item):
                    ev, st, ts = item
                    if sleep_sec > 0:
                        time.sleep(sleep_sec)
                    return _fetch_prop_snapshot(ev, st, books, ts, sport, dry_run=False)

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    # Submit up to budget
                    budget = max_requests - request_count if max_requests > 0 else len(work_items)
                    batch = work_items[:budget]
                    futures = {pool.submit(_fetch_one, item): item for item in batch}
                    for fut in as_completed(futures):
                        rows, quota = fut.result()
                        with _lock:
                            request_count += 1
                            if quota.get("remaining") is not None:
                                quota_remaining = quota["remaining"]
                            if rows:
                                saved = store.upsert_snapshots(rows)
                                day_snaps += saved
            else:
                # Sequential fallback (dry-run, single worker, or single item)
                for event, stat, ts_str in work_items:
                    if max_requests > 0 and request_count >= max_requests:
                        break
                    rows, quota = _fetch_prop_snapshot(
                        event, stat, books, ts_str, sport, dry_run=dry_run
                    )
                    request_count += 1
                    if quota.get("remaining") is not None:
                        quota_remaining = quota["remaining"]
                    if not dry_run and rows:
                        saved = store.upsert_snapshots(rows)
                        day_snaps += saved
                    elif dry_run:
                        day_snaps += len(rows)
                    if not dry_run and sleep_sec > 0:
                        time.sleep(sleep_sec)

            total_snaps += day_snaps
            quota_msg = f"  quota_remaining={quota_remaining}" if quota_remaining is not None else ""
            print(
                f"    -> {day_snaps} snapshots  ({request_count} API calls total){quota_msg}",
                flush=True,
            )

        store.finish_run(run_id, status="done")
        result = {
            "success":        True,
            "runId":          run_id,
            "dateFrom":       date_from,
            "dateTo":         date_to,
            "requestCount":   request_count,
            "totalSnapshots": total_snaps,
            "errors":         errors,
            "dryRun":         dry_run,
        }
        if quota_remaining is not None:
            result["quotaRemaining"] = quota_remaining

    except Exception as e:
        store.finish_run(run_id, status="error", error=str(e))
        result = {"success": False, "runId": run_id, "error": str(e),
                  "requestCount": request_count, "totalSnapshots": total_snaps}
    finally:
        store.close()

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Backfill historical Odds API snapshots")
    p.add_argument("--date-from",        required=True,  help="Start date YYYY-MM-DD")
    p.add_argument("--date-to",          required=True,  help="End date YYYY-MM-DD")
    p.add_argument("--books",            default=_BOOK_DEFAULTS)
    p.add_argument("--stats",            default=",".join(_STAT_DEFAULTS))
    p.add_argument("--snap-offsets",      default=None,
                   help="Comma-separated minutes-before-tip to snapshot, e.g. 10,60,120,240. "
                        "Overrides --offset-minutes/--interval-minutes when provided.")
    p.add_argument("--offset-minutes",   type=int, default=60,
                   help="Minutes before game start to snapshot (default 60, ignored if --snap-offsets set)")
    p.add_argument("--interval-minutes", type=int, default=0,
                   help="Extra snapshots every N min before offset; 0=single (default, ignored if --snap-offsets set)")
    p.add_argument("--sport",            default="basketball_nba")
    p.add_argument("--max-requests",     type=int, default=0,
                   help="Hard cap on API calls; 0=unlimited (default)")
    p.add_argument("--resume",           action="store_true",
                   help="Skip dates already in the DB")
    p.add_argument("--dry-run",          action="store_true",
                   help="Print what would be fetched without making API calls")
    p.add_argument("--db",               default=None,
                   help="Override SQLite DB path")
    p.add_argument("--workers",         type=int, default=1,
                   help="Concurrent API workers (default 1; try 4 for ~3x speedup)")
    p.add_argument("--sleep-sec",       type=float, default=0.05,
                   help="Throttle between requests in seconds (default 0.05)")
    args = p.parse_args()

    stats = [s.strip() for s in args.stats.split(",") if s.strip()]
    snap_offsets = (
        [int(x.strip()) for x in args.snap_offsets.split(",") if x.strip()]
        if args.snap_offsets else None
    )
    result = run_backfill(
        date_from=args.date_from,
        date_to=args.date_to,
        books=args.books,
        stats=stats,
        offset_minutes=args.offset_minutes,
        interval_minutes=args.interval_minutes,
        snap_offsets=snap_offsets,
        sport=args.sport,
        max_requests=args.max_requests,
        resume=args.resume,
        dry_run=args.dry_run,
        db_path=args.db,
        workers=args.workers,
        sleep_sec=args.sleep_sec,
    )
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
