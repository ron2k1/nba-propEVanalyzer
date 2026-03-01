#!/usr/bin/env python3
"""
CLI handlers for Odds API backfill, line bridge, and coverage commands.

Commands:
  odds_backfill      <date_from> <date_to> [--books] [--stats] ...
  odds_build_closes  [date_from] [date_to] [--db path]
  odds_coverage      [--by-date date_from date_to] [--db path]
  line_bridge        [date_from] [date_to] [--dry-run] [--books] [--stats]
  sportsdata_backfill <date_from> <date_to> [--seasons] [--max-requests 100]
"""

from datetime import datetime
from pathlib import Path

from .shared import _looks_like_date

_CLI_ROOT = Path(__file__).resolve().parent.parent


def _handle_odds_backfill(argv):
    from scripts.backfill_odds_history import run_backfill

    if len(argv) < 4:
        return {
            "error": (
                "Usage: odds_backfill <date_from:YYYY-MM-DD> <date_to:YYYY-MM-DD> "
                "[--books b1,b2] [--stats s1,s2] [--offset-minutes 60] "
                "[--interval-minutes 0] [--max-requests 0] [--resume] [--dry-run] "
                "[--db <path>]"
            )
        }

    date_from = argv[2]
    date_to   = argv[3]
    books     = "betmgm,draftkings,fanduel"
    stats     = ["pts", "ast", "pra"]  # align with BETTING_POLICY whitelist for real-line coverage
    offset_minutes   = 60
    interval_minutes = 0
    snap_offsets     = None
    max_requests     = 0
    resume   = False
    dry_run  = False
    db_path  = None

    idx = 4
    while idx < len(argv):
        tok = argv[idx]
        if tok == "--books" and idx + 1 < len(argv):
            books = argv[idx + 1]; idx += 2
        elif tok == "--stats" and idx + 1 < len(argv):
            stats = [s.strip() for s in argv[idx + 1].split(",") if s.strip()]; idx += 2
        elif tok == "--snap-offsets" and idx + 1 < len(argv):
            try:
                snap_offsets = [int(x.strip()) for x in argv[idx + 1].split(",") if x.strip()]
            except ValueError:
                pass
            idx += 2
        elif tok == "--offset-minutes" and idx + 1 < len(argv):
            try:
                offset_minutes = int(argv[idx + 1])
            except ValueError:
                pass
            idx += 2
        elif tok == "--interval-minutes" and idx + 1 < len(argv):
            try:
                interval_minutes = int(argv[idx + 1])
            except ValueError:
                pass
            idx += 2
        elif tok == "--max-requests" and idx + 1 < len(argv):
            try:
                max_requests = int(argv[idx + 1])
            except ValueError:
                pass
            idx += 2
        elif tok == "--resume":
            resume = True; idx += 1
        elif tok == "--dry-run":
            dry_run = True; idx += 1
        elif tok == "--db" and idx + 1 < len(argv):
            db_path = argv[idx + 1]; idx += 2
        else:
            idx += 1

    return run_backfill(
        date_from=date_from,
        date_to=date_to,
        books=books,
        stats=stats,
        offset_minutes=offset_minutes,
        interval_minutes=interval_minutes,
        snap_offsets=snap_offsets,
        max_requests=max_requests,
        resume=resume,
        dry_run=dry_run,
        db_path=db_path,
    )


def _handle_odds_build_closes(argv):
    from scripts.build_closing_lines import build_closing_lines
    from core.nba_odds_store import OddsStore

    date_from = None
    date_to   = None
    db_path   = None

    idx = 2
    while idx < len(argv):
        tok = argv[idx]
        if _looks_like_date(tok) and date_from is None:
            date_from = tok
        elif _looks_like_date(tok):
            date_to = tok
        elif tok == "--db" and idx + 1 < len(argv):
            db_path = argv[idx + 1]; idx += 1
        idx += 1

    store = OddsStore(db_path=db_path)
    saved, total = build_closing_lines(store, date_from, date_to)
    store.close()
    return {
        "success":  True,
        "derived":  total,
        "saved":    saved,
        "dateFrom": date_from,
        "dateTo":   date_to,
    }


def _handle_odds_coverage(argv):
    from core.nba_odds_store import OddsStore

    db_path   = None
    date_from = None
    date_to   = None
    by_date   = False

    idx = 2
    while idx < len(argv):
        tok = argv[idx]
        if tok == "--db" and idx + 1 < len(argv):
            db_path = argv[idx + 1]
            idx += 2
        elif tok == "--by-date" and idx + 2 < len(argv):
            by_date = True
            date_from = argv[idx + 1]
            date_to = argv[idx + 2]
            idx += 3
        else:
            idx += 1

    store = OddsStore(db_path=db_path)
    result = store.coverage_summary()
    if by_date and date_from and date_to:
        by_date_res = store.coverage_by_date(date_from, date_to)
        result["coverageByDate"] = by_date_res.get("byDate", [])
        result["totalClosingInRange"] = by_date_res.get("totalClosingRows", 0)
    store.close()
    return result


def _handle_line_bridge(argv):
    from scripts.line_to_odds_bridge import run_bridge

    today = datetime.now().strftime("%Y-%m-%d")
    date_from = today
    date_to = today
    dry_run = "--dry-run" in argv
    books = None
    stats = None
    line_dir = None
    db_path = None
    date_tokens = []

    idx = 2
    while idx < len(argv):
        tok = argv[idx]
        if tok == "--dry-run":
            idx += 1
        elif tok == "--books" and idx + 1 < len(argv):
            books = argv[idx + 1]
            idx += 2
        elif tok == "--stats" and idx + 1 < len(argv):
            stats = argv[idx + 1]
            idx += 2
        elif tok == "--line-dir" and idx + 1 < len(argv):
            line_dir = argv[idx + 1]
            idx += 2
        elif tok == "--db" and idx + 1 < len(argv):
            db_path = argv[idx + 1]
            idx += 2
        elif _looks_like_date(tok):
            date_tokens.append(tok)
            idx += 1
        else:
            return {
                "error": (
                    f"Usage: line_bridge [date_from:YYYY-MM-DD] [date_to:YYYY-MM-DD] "
                    f"[--dry-run] [--books b1,b2] [--stats s1,s2] [--line-dir <path>] [--db <path>]\n"
                    f"Example: line_bridge {today} --dry-run"
                )
            }

    if len(date_tokens) >= 1:
        date_from = date_tokens[0]
        date_to = date_from
    if len(date_tokens) >= 2:
        date_to = date_tokens[1]
    if len(date_tokens) > 2:
        return {
            "error": (
                "Usage: line_bridge [date_from:YYYY-MM-DD] [date_to:YYYY-MM-DD] "
                "[--dry-run] [--books b1,b2] [--stats s1,s2] [--line-dir <path>] [--db <path>]"
            )
        }

    return run_bridge(
        date_from=date_from,
        date_to=date_to,
        line_history_dir=line_dir,
        odds_db_path=db_path,
        books=books,
        stats=stats,
        dry_run=dry_run,
    )


def _handle_sportsdata_backfill(argv):
    from scripts.backfill_sportsdataio import run_backfill

    if len(argv) < 4:
        return {
            "error": (
                "Usage: sportsdata_backfill <date_from:YYYY-MM-DD> <date_to:YYYY-MM-DD> "
                "[--season-from 2023] [--season-to 2026] [--seasons 2023,2024,2025,2026] "
                "[--max-requests 100] [--sleep-sec 0.15] [--no-line-movement] "
                "[--requested-only] [--no-skip-empty-dates] "
                "[--no-resume] [--dry-run] [--out-dir <path>]"
            )
        }

    date_from = argv[2]
    date_to = argv[3]
    season_from = None
    season_to = None
    seasons_csv = None
    max_requests = 0
    sleep_sec = 0.15
    include_line_movement = True
    requested_only = False
    skip_empty_dates = True
    resume = True
    dry_run = False
    out_dir = None

    idx = 4
    while idx < len(argv):
        tok = argv[idx]
        if tok == "--season-from" and idx + 1 < len(argv):
            try:
                season_from = int(argv[idx + 1])
            except ValueError:
                pass
            idx += 2
        elif tok == "--season-to" and idx + 1 < len(argv):
            try:
                season_to = int(argv[idx + 1])
            except ValueError:
                pass
            idx += 2
        elif tok == "--seasons" and idx + 1 < len(argv):
            seasons_csv = argv[idx + 1]
            idx += 2
        elif tok == "--max-requests" and idx + 1 < len(argv):
            try:
                max_requests = int(argv[idx + 1])
            except ValueError:
                pass
            idx += 2
        elif tok == "--sleep-sec" and idx + 1 < len(argv):
            try:
                sleep_sec = float(argv[idx + 1])
            except ValueError:
                pass
            idx += 2
        elif tok == "--no-line-movement":
            include_line_movement = False
            idx += 1
        elif tok == "--requested-only":
            requested_only = True
            idx += 1
        elif tok == "--no-skip-empty-dates":
            skip_empty_dates = False
            idx += 1
        elif tok == "--no-resume":
            resume = False
            idx += 1
        elif tok == "--dry-run":
            dry_run = True
            idx += 1
        elif tok == "--out-dir" and idx + 1 < len(argv):
            out_dir = argv[idx + 1]
            idx += 2
        else:
            idx += 1

    return run_backfill(
        date_from=date_from,
        date_to=date_to,
        out_dir=out_dir or (_CLI_ROOT / "data" / "reference" / "sportsdataio" / "raw"),
        season_from=season_from,
        season_to=season_to,
        seasons_csv=seasons_csv,
        include_line_movement=include_line_movement,
        requested_only=requested_only,
        skip_empty_dates=skip_empty_dates,
        max_requests=max_requests,
        sleep_sec=sleep_sec,
        resume=resume,
        dry_run=dry_run,
    )


_COMMANDS = {
    "odds_backfill":       _handle_odds_backfill,
    "odds_build_closes":   _handle_odds_build_closes,
    "odds_coverage":       _handle_odds_coverage,
    "line_bridge":         _handle_line_bridge,
    "sportsdata_backfill": _handle_sportsdata_backfill,
}


def handle_odds_command(command, argv):  # shim — router no longer calls this
    h = _COMMANDS.get(command)
    return h(argv) if h else None
