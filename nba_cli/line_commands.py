#!/usr/bin/env python3
"""
CLI handlers for line-history, CLV evaluation, and injury alerts.

Commands:
  collect_lines   [--books b1,b2] [--stats s1,s2] [--stale]
  line_history    [date YYYY-MM-DD] [player_name] [stat]
  clv_eval        [date YYYY-MM-DD]
  stale_lines     [date YYYY-MM-DD] [--min-diff 0.5]
  injury_alerts   [date YYYY-MM-DD]
  line_bridge     [date_from] [date_to] [--dry-run] [--books] [--stats]
  sportsdata_backfill <date_from> <date_to> [--seasons 2024,2025] [--max-requests 100]
  odds_coverage   [--by-date date_from date_to] [--db path]
  odds_backfill   date_from date_to [--books] [--stats] ...
  odds_build_closes [date_from] [date_to]
  props_scan      [date YYYY-MM-DD] [min_edge] — scan all player over/under from LineStore
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_CLI_ROOT = Path(__file__).resolve().parent.parent

from core.nba_line_store import LineStore


def handle_line_command(command, argv):
    # -----------------------------------------------------------------------
    # collect_lines
    # -----------------------------------------------------------------------
    if command == "collect_lines":
        from core.nba_data_collection import get_todays_event_props_bulk

        books = "betmgm,draftkings,fanduel"
        stats = ["pts", "reb", "ast", "fg3m", "tov"]
        show_stale = False

        idx = 2
        while idx < len(argv):
            tok = argv[idx]
            if tok == "--books" and idx + 1 < len(argv):
                books = argv[idx + 1]
                idx  += 2
            elif tok == "--stats" and idx + 1 < len(argv):
                raw = argv[idx + 1].strip().lower()
                stats = (
                    ["all"] if raw == "all"
                    else [s.strip() for s in raw.split(",") if s.strip()]
                )
                idx += 2
            elif tok == "--stale":
                show_stale = True
                idx += 1
            else:
                idx += 1

        result = get_todays_event_props_bulk(bookmakers=books, stats=stats)
        if not result.get("success"):
            return {"success": False, "error": result.get("error")}

        store     = LineStore()
        snapshots = result.get("snapshots", [])
        saved     = store.append_snapshots(snapshots)

        out = {
            "success":       True,
            "timestamp":     result.get("timestamp"),
            "eventCount":    result.get("eventCount", 0),
            "snapshotCount": saved,
            "errors":        result.get("errors", []),
            "quota":         result.get("quota"),
        }

        if show_stale and saved > 0:
            date_str = str(result.get("timestamp", ""))[:10]
            stale    = store.detect_stale_lines(date_str)
            out["staleCount"] = len(stale)
            out["stale"]      = stale[:20]

        return out

    # -----------------------------------------------------------------------
    # props_scan — scan all player over/under from LineStore (uses offline_scan)
    # -----------------------------------------------------------------------
    if command == "props_scan":
        date_str = datetime.now().strftime("%Y-%m-%d")
        min_edge = 0.03
        for tok in argv[2:]:
            if _looks_like_date(tok):
                date_str = tok
                break
        for tok in argv[2:]:
            try:
                min_edge = float(tok)
                break
            except (TypeError, ValueError):
                pass
        script = _CLI_ROOT / "scripts" / "offline_scan.py"
        if not script.exists():
            return {"success": False, "error": f"offline_scan.py not found at {script}"}
        try:
            proc = subprocess.run(
                [sys.executable, str(script), date_str, str(min_edge)],
                cwd=str(_CLI_ROOT),
                capture_output=True,
                text=True,
                timeout=600,
            )
            out = proc.stdout or ""
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if not lines:
                return {
                    "success": False,
                    "error": proc.stderr or "No output from props_scan",
                }
            last = lines[-1]
            try:
                payload = json.loads(last)
                if proc.returncode != 0:
                    payload["success"] = False
                    payload.setdefault("stderr", proc.stderr)
                return payload
            except json.JSONDecodeError:
                return {"success": False, "error": f"Invalid JSON: {last[:200]}", "stderr": proc.stderr}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "props_scan timed out (600s)"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # -----------------------------------------------------------------------
    # line_history
    # -----------------------------------------------------------------------
    if command == "line_history":
        store    = LineStore()
        date_str = datetime.now().strftime("%Y-%m-%d")
        player   = None
        stat     = None

        idx = 2
        while idx < len(argv):
            tok = argv[idx]
            if _looks_like_date(tok):
                date_str = tok
            elif tok in {"pts","reb","ast","fg3m","stl","blk","tov","pra","pr","pa","ra"}:
                stat = tok
            elif not tok.startswith("-"):
                player = tok
            idx += 1

        snapshots = store.get_snapshots(date_str, stat=stat, player_name=player)
        return {
            "success":       True,
            "date":          date_str,
            "playerFilter":  player,
            "statFilter":    stat,
            "snapshotCount": len(snapshots),
            "snapshots":     snapshots,
        }

    # -----------------------------------------------------------------------
    # clv_eval
    # -----------------------------------------------------------------------
    if command == "clv_eval":
        store    = LineStore()
        date_str = datetime.now().strftime("%Y-%m-%d")

        for tok in argv[2:]:
            if _looks_like_date(tok):
                date_str = tok
                break

        return store.clv_summary_for_date(date_str)

    # -----------------------------------------------------------------------
    # stale_lines
    # -----------------------------------------------------------------------
    if command == "stale_lines":
        store    = LineStore()
        date_str = datetime.now().strftime("%Y-%m-%d")
        min_diff = 0.5

        idx = 2
        while idx < len(argv):
            tok = argv[idx]
            if _looks_like_date(tok):
                date_str = tok
            elif tok == "--min-diff" and idx + 1 < len(argv):
                try:
                    min_diff = float(argv[idx + 1])
                except ValueError:
                    pass
                idx += 1
            idx += 1

        stale = store.detect_stale_lines(date_str, min_line_diff=min_diff)
        return {
            "success":     True,
            "date":        date_str,
            "minLineDiff": min_diff,
            "staleCount":  len(stale),
            "stale":       stale,
        }

    # -----------------------------------------------------------------------
    # minutes_eval
    # -----------------------------------------------------------------------
    if command == "minutes_eval":
        from core.nba_backtest import run_minutes_eval

        if len(argv) < 3:
            return {
                "error": (
                    "Usage: minutes_eval <date_from:YYYY-MM-DD> [date_to:YYYY-MM-DD] "
                    "[--local] [--data-source nba|bref|local] [--local-index <path>]"
                )
            }

        date_from   = argv[2]
        date_to     = None
        data_source = "nba"
        local_index = None

        idx = 3
        if idx < len(argv) and not str(argv[idx]).startswith("-"):
            date_to = argv[idx]
            idx += 1

        while idx < len(argv):
            tok = str(argv[idx]).strip().lower()
            if tok == "--local":
                data_source = "local"
            elif tok == "--data-source" and idx + 1 < len(argv):
                data_source = str(argv[idx + 1]).strip().lower()
                idx += 1
            elif tok == "--local-index" and idx + 1 < len(argv):
                local_index = str(argv[idx + 1]).strip()
                idx += 1
            idx += 1

        return run_minutes_eval(
            date_from=date_from,
            date_to=date_to,
            data_source=data_source,
            local_index=local_index,
        )

    # -----------------------------------------------------------------------
    # injury_alerts
    # -----------------------------------------------------------------------
    if command == "injury_alerts":
        store    = LineStore()
        date_str = datetime.now().strftime("%Y-%m-%d")

        for tok in argv[2:]:
            if _looks_like_date(tok):
                date_str = tok
                break

        alerts = store.get_alerts(date_str)
        return {
            "success":     True,
            "date":        date_str,
            "alertCount":  len(alerts),
            "alerts":      alerts,
        }

    # -----------------------------------------------------------------------
    # odds_backfill
    # -----------------------------------------------------------------------
    if command == "odds_backfill":
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

    # -----------------------------------------------------------------------
    # odds_build_closes
    # -----------------------------------------------------------------------
    if command == "odds_build_closes":
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

    # -----------------------------------------------------------------------
    # sportsdata_backfill
    # -----------------------------------------------------------------------
    if command == "sportsdata_backfill":
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

    # -----------------------------------------------------------------------
    # line_bridge
    # -----------------------------------------------------------------------
    if command == "line_bridge":
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

    # -----------------------------------------------------------------------
    # odds_coverage
    # -----------------------------------------------------------------------
    if command == "odds_coverage":
        from core.nba_odds_store import OddsStore

        db_path = None
        date_from = None
        date_to = None
        by_date = False

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

    return None


def _looks_like_date(s: str) -> bool:
    try:
        datetime.strptime(str(s or "")[:10], "%Y-%m-%d")
        return True
    except ValueError:
        return False
