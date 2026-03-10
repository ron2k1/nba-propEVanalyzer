#!/usr/bin/env python3
"""
Event-relative dense odds collector.

Collects player prop snapshots at multiple offsets before each game's tipoff
to maximize closing-line freshness. Runs from ~3 PM ET until all games complete.

After all collections finish, runs line_bridge + odds_build_closes to push
JSONL snapshots into OddsStore SQLite and derive closing lines for CLV.

Usage
-----
.venv/Scripts/python.exe scripts/dense_collector.py --dry-run
.venv/Scripts/python.exe scripts/dense_collector.py --max-requests 50
.venv/Scripts/python.exe scripts/dense_collector.py --books betmgm,draftkings,fanduel,pinnacle --stats pts,ast,reb,pra,fg3m,stl,blk
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nba_data_collection import (
    get_event_player_props_bulk,
    _odds_api_get,
    ODDS_DEFAULT_SPORT,
)
from scripts.collect_lines import LineStore

_log = logging.getLogger("dense_collector")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)

LOG_DIR = ROOT / "data" / "logs"
LOG_PATH = LOG_DIR / "dense_collector.jsonl"
STATE_PATH = LOG_DIR / "dense_collector_state.json"

DEFAULT_OFFSETS_MIN = [180, 120, 90, 60, 45, 30, 20, 10, 5]
DEFAULT_BOOKS = "betmgm,draftkings,fanduel,pinnacle"
DEFAULT_STATS = "pts,ast,reb,pra,fg3m,stl,blk"
MERGE_WINDOW_SEC = 120  # collapse collection windows within 2 min


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now():
    return datetime.now(UTC)


def _utc_now_iso():
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _append_jsonl(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")


def _save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")


def _load_state() -> dict | None:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


# ---------------------------------------------------------------------------
# Event discovery
# ---------------------------------------------------------------------------

def _discover_events(sport: str = ODDS_DEFAULT_SPORT, books: str = DEFAULT_BOOKS) -> list[dict]:
    """
    Discover today's NBA events via the h2h odds endpoint (1 API credit).
    Returns list of {event_id, commence_time, home_team, away_team}.
    """
    resp = _odds_api_get(
        f"sports/{sport}/odds",
        params={
            "regions": "us",
            "markets": "h2h",
            "oddsFormat": "american",
            "dateFormat": "iso",
            "bookmakers": books.split(",")[0],  # only need one book for discovery
        },
        timeout=30,
    )
    if not resp.get("success"):
        _log.error("Event discovery failed: %s", resp.get("error"))
        return []

    events = []
    for ev in resp.get("data", []) or []:
        commence = ev.get("commence_time")
        if not commence:
            continue
        events.append({
            "event_id": ev["id"],
            "commence_time": commence,
            "home_team": ev.get("home_team", ""),
            "away_team": ev.get("away_team", ""),
        })

    _log.info("Discovered %d events (quota remaining: %s)",
              len(events), resp.get("quota", {}).get("remaining", "?"))
    return events


# ---------------------------------------------------------------------------
# Schedule builder
# ---------------------------------------------------------------------------

def _parse_commence(ct: str) -> datetime:
    """Parse ISO commence_time to aware UTC datetime."""
    if ct.endswith("Z"):
        ct = ct[:-1] + "+00:00"
    return datetime.fromisoformat(ct).astimezone(UTC)


def _nba_date_from_commence(ct: str) -> str | None:
    try:
        return (_parse_commence(ct) - timedelta(hours=6)).date().isoformat()
    except Exception:
        return None


def _build_schedule(
    events: list[dict],
    offsets_min: list[int] | None = None,
    merge_sec: int = MERGE_WINDOW_SEC,
) -> list[tuple[datetime, list[str]]]:
    """
    Build a collection schedule: for each event, compute collection times
    at each offset before tipoff. Merge windows within merge_sec.

    Returns sorted list of (collection_time_utc, [event_ids]).
    """
    if offsets_min is None:
        offsets_min = DEFAULT_OFFSETS_MIN

    # Build raw collection points
    raw_points: list[tuple[datetime, str]] = []
    now = _utc_now()

    for ev in events:
        tip = _parse_commence(ev["commence_time"])
        for offset in offsets_min:
            collect_at = tip - timedelta(minutes=offset)
            if collect_at > now - timedelta(minutes=1):  # allow 1 min grace for just-passed
                raw_points.append((collect_at, ev["event_id"]))

    if not raw_points:
        return []

    # Sort by time
    raw_points.sort(key=lambda x: x[0])

    # Merge windows within merge_sec
    schedule: list[tuple[datetime, list[str]]] = []
    current_time = raw_points[0][0]
    current_ids: list[str] = [raw_points[0][1]]

    for pt_time, pt_id in raw_points[1:]:
        if (pt_time - current_time).total_seconds() <= merge_sec:
            if pt_id not in current_ids:
                current_ids.append(pt_id)
        else:
            schedule.append((current_time, current_ids))
            current_time = pt_time
            current_ids = [pt_id]

    schedule.append((current_time, current_ids))
    return schedule


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def _collect_for_events(
    event_ids: list[str],
    all_events: dict[str, dict],
    books: str,
    stats: list[str],
    store: LineStore,
    workers: int = 1,
) -> dict:
    """
    Collect player props for given events. Returns summary dict.
    """
    total_snaps = 0
    api_calls = 0
    errors = []
    ts = _utc_now_iso()

    # Build work items
    work_items = [(eid, stat) for eid in event_ids for stat in stats]

    if workers > 1 and len(work_items) > 1:
        _lock = threading.Lock()

        def _fetch_one(item):
            eid, stat = item
            return eid, stat, get_event_player_props_bulk(
                event_id=eid, stat=stat, bookmakers=books, sport=ODDS_DEFAULT_SPORT,
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_one, item): item for item in work_items}
            for fut in as_completed(futures):
                eid, stat, result = fut.result()
                ev = all_events.get(eid, {})
                with _lock:
                    api_calls += 1
                    if not result.get("success"):
                        errors.append({"event_id": eid, "stat": stat, "error": result.get("error")})
                        _log.warning("Failed %s/%s: %s", eid, stat, result.get("error"))
                        continue
                    snapshots = result.get("snapshots", [])
                    if snapshots:
                        for snap in snapshots:
                            snap["timestamp_utc"] = ts
                            snap["commence_time"] = ev.get("commence_time", "")
                            snap["home_team_abbr"] = ""
                            snap["away_team_abbr"] = ""
                        store.append_snapshots(snapshots)
                        total_snaps += len(snapshots)
                    _log.debug("  %s/%s: %d snapshots", eid, stat, len(snapshots))
    else:
        for eid, stat in work_items:
            ev = all_events.get(eid, {})
            result = get_event_player_props_bulk(
                event_id=eid, stat=stat, bookmakers=books, sport=ODDS_DEFAULT_SPORT,
            )
            api_calls += 1
            if not result.get("success"):
                errors.append({"event_id": eid, "stat": stat, "error": result.get("error")})
                _log.warning("Failed %s/%s: %s", eid, stat, result.get("error"))
                continue
            snapshots = result.get("snapshots", [])
            if snapshots:
                for snap in snapshots:
                    snap["timestamp_utc"] = ts
                    snap["commence_time"] = ev.get("commence_time", "")
                    snap["home_team_abbr"] = ""
                    snap["away_team_abbr"] = ""
                store.append_snapshots(snapshots)
                total_snaps += len(snapshots)
            _log.debug("  %s/%s: %d snapshots", eid, stat, len(snapshots))

    return {
        "api_calls": api_calls,
        "snapshots": total_snaps,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Bridge + Build pipeline
# ---------------------------------------------------------------------------

def _run_bridge_and_build(date_from: str | None = None, date_to: str | None = None) -> dict:
    """
    Run line_bridge then odds_build_closes for today's date.
    This converts JSONL snapshots → OddsStore SQLite → closing lines.
    """
    from scripts.line_to_odds_bridge import run_bridge
    from scripts.build_closing_lines import build_closing_lines
    from core.nba_odds_store import OddsStore

    default_date = (_utc_now() - timedelta(hours=6)).date().isoformat()
    date_from = date_from or default_date
    date_to = date_to or date_from

    _log.info("Running line_bridge for %s -> %s", date_from, date_to)
    bridge_result = run_bridge(date_from=date_from, date_to=date_to)
    _log.info("Bridge: inserted=%s, skipped=%s, errors=%s",
              bridge_result.get("inserted", 0),
              bridge_result.get("skipped", 0),
              bridge_result.get("errors", 0))

    _log.info("Running odds_build_closes for %s -> %s", date_from, date_to)
    store = OddsStore()
    saved, total = build_closing_lines(store, date_from=date_from, date_to=date_to)
    store.close()
    _log.info("Build closes: derived=%d, saved=%d", total, saved)

    return {
        "bridge": bridge_result,
        "build_closes": {"derived": total, "saved": saved},
    }


# ---------------------------------------------------------------------------
# Main schedule loop
# ---------------------------------------------------------------------------

def _run_schedule_loop(
    schedule: list[tuple[datetime, list[str]]],
    all_events: dict[str, dict],
    books: str,
    stats: list[str],
    store: LineStore,
    max_requests: int = 2000,
    dry_run: bool = False,
    workers: int = 1,
) -> dict:
    """
    Execute the collection schedule. Sleep until each window, then collect.
    """
    total_api_calls = 0
    total_snaps = 0
    windows_completed = 0

    _log.info("Schedule has %d collection windows", len(schedule))

    for i, (collect_at, event_ids) in enumerate(schedule):
        # Check budget
        estimated_calls = len(event_ids) * len(stats)
        if max_requests > 0 and total_api_calls + estimated_calls > max_requests:
            _log.warning("Budget cap reached: %d/%d calls used. Stopping.", total_api_calls, max_requests)
            break

        now = _utc_now()
        wait_sec = (collect_at - now).total_seconds()

        if dry_run:
            et_time = collect_at.astimezone(timezone(timedelta(hours=-4)))
            _log.info(
                "[DRY-RUN] Window %d/%d at %s ET — %d events × %d stats = ~%d calls",
                i + 1, len(schedule), et_time.strftime("%H:%M:%S"),
                len(event_ids), len(stats), estimated_calls,
            )
            total_api_calls += estimated_calls
            windows_completed += 1
            continue

        if wait_sec > 0:
            _log.info(
                "Window %d/%d in %.0fs (%d events × %d stats)",
                i + 1, len(schedule), wait_sec, len(event_ids), len(stats),
            )
            time.sleep(wait_sec)

        _log.info("Collecting window %d/%d (%d events)", i + 1, len(schedule), len(event_ids))
        result = _collect_for_events(event_ids, all_events, books, stats, store, workers=workers)
        total_api_calls += result["api_calls"]
        total_snaps += result["snapshots"]
        windows_completed += 1

        # Save state for crash recovery
        _save_state({
            "last_window": i,
            "total_windows": len(schedule),
            "total_api_calls": total_api_calls,
            "total_snaps": total_snaps,
            "ts": _utc_now_iso(),
        })

        # Log progress
        _append_jsonl(LOG_PATH, {
            "ts": _utc_now_iso(),
            "window": i + 1,
            "events": len(event_ids),
            "api_calls": result["api_calls"],
            "snapshots": result["snapshots"],
            "errors": len(result["errors"]),
            "cumulative_calls": total_api_calls,
        })

    return {
        "windows_completed": windows_completed,
        "total_windows": len(schedule),
        "total_api_calls": total_api_calls,
        "total_snapshots": total_snaps,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Dense near-tipoff odds collector")
    parser.add_argument("--books", default=DEFAULT_BOOKS)
    parser.add_argument("--stats", default=DEFAULT_STATS)
    parser.add_argument("--offsets", default=",".join(str(o) for o in DEFAULT_OFFSETS_MIN),
                        help="Comma-separated minutes-before-tip offsets (default: 180,120,90,60,45,30,20,10,5)")
    parser.add_argument("--max-requests", type=int, default=2000,
                        help="Hard cap on API calls (default: 2000)")
    parser.add_argument("--refresh-interval", type=int, default=60,
                        help="Minutes between event re-discovery (default: 60)")
    parser.add_argument("--skip-bridge", action="store_true",
                        help="Skip end-of-day line_bridge + odds_build_closes")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print schedule without making API calls or sleeping")
    parser.add_argument("--workers", type=int, default=4,
                        help="Concurrent API workers per window (default: 4)")
    parser.add_argument("--sport", default=ODDS_DEFAULT_SPORT)
    args = parser.parse_args()

    stats = [s.strip() for s in args.stats.split(",") if s.strip()]
    offsets = [int(o.strip()) for o in args.offsets.split(",") if o.strip()]

    _log.info("Dense collector starting — books=%s, stats=%s, offsets=%s, max_requests=%d",
              args.books, stats, offsets, args.max_requests)

    # Discover events
    events = _discover_events(sport=args.sport, books=args.books)
    if not events:
        _log.warning("No events found. Exiting.")
        result = {"success": True, "events": 0, "message": "No events today"}
        print(json.dumps(result))
        return 0

    events_map = {ev["event_id"]: ev for ev in events}

    # Build schedule
    schedule = _build_schedule(events, offsets_min=offsets)
    if not schedule:
        _log.warning("No collection windows in the future. All games may have started.")
        result = {"success": True, "events": len(events), "windows": 0, "message": "No future windows"}
        print(json.dumps(result))
        return 0

    # Print schedule summary
    for i, (ct, eids) in enumerate(schedule):
        et = ct.astimezone(timezone(timedelta(hours=-4)))
        _log.info("  Window %d: %s ET — %d events", i + 1, et.strftime("%H:%M:%S"), len(eids))

    # Open LineStore
    store = LineStore()

    # Run schedule
    summary = _run_schedule_loop(
        schedule=schedule,
        all_events=events_map,
        books=args.books,
        stats=stats,
        store=store,
        max_requests=args.max_requests,
        dry_run=args.dry_run,
        workers=args.workers,
    )

    _log.info("Collection complete: %d/%d windows, %d API calls, %d snapshots",
              summary["windows_completed"], summary["total_windows"],
              summary["total_api_calls"], summary["total_snapshots"])

    # End-of-day bridge + build
    bridge_result = None
    if not args.skip_bridge and not args.dry_run:
        try:
            event_dates = sorted({
                d for d in (_nba_date_from_commence(ev.get("commence_time", "")) for ev in events) if d
            })
            bridge_result = _run_bridge_and_build(
                date_from=event_dates[0] if event_dates else None,
                date_to=event_dates[-1] if event_dates else None,
            )
        except Exception as e:
            _log.error("Bridge+build failed: %s", e)
            bridge_result = {"error": str(e)}

    # Final output
    result = {
        "success": True,
        "events": len(events),
        **summary,
        "bridge_and_build": bridge_result,
    }

    # Log run
    _append_jsonl(LOG_PATH, {"ts": _utc_now_iso(), "type": "run_complete", **result})

    # Clean up state file on successful completion
    if STATE_PATH.exists():
        STATE_PATH.unlink()

    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
