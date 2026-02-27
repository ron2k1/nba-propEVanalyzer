#!/usr/bin/env python3
"""
CLI handlers for line-history, CLV evaluation, and injury alerts.

Commands:
  collect_lines  [--books b1,b2] [--stats s1,s2] [--stale]
  line_history   [date YYYY-MM-DD] [player_name] [stat]
  clv_eval       [date YYYY-MM-DD]
  stale_lines    [date YYYY-MM-DD] [--min-diff 0.5]
  injury_alerts  [date YYYY-MM-DD]
"""

from datetime import datetime

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
        from nba_backtest import run_minutes_eval

        if len(argv) < 3:
            return {
                "error": (
                    "Usage: minutes_eval <date_from:YYYY-MM-DD> [date_to:YYYY-MM-DD] "
                    "[--local] [--data-source nba|bref|local]"
                )
            }

        date_from   = argv[2]
        date_to     = None
        data_source = "nba"

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
            idx += 1

        return run_minutes_eval(date_from=date_from, date_to=date_to, data_source=data_source)

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

    return None


def _looks_like_date(s: str) -> bool:
    try:
        datetime.strptime(str(s or "")[:10], "%Y-%m-%d")
        return True
    except ValueError:
        return False
