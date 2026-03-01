#!/usr/bin/env python3
"""
CLI handlers for line-history, CLV evaluation, and injury alerts.

Commands:
  collect_lines   [--books b1,b2] [--stats s1,s2] [--stale]
  line_history    [date YYYY-MM-DD] [player_name] [stat]
  clv_eval        [date YYYY-MM-DD]
  stale_lines     [date YYYY-MM-DD] [--min-diff 0.5]
  injury_alerts   [date YYYY-MM-DD]
  props_scan      [date YYYY-MM-DD] [min_edge] — scan all player over/under from LineStore
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from core.nba_line_store import LineStore

from .shared import _looks_like_date

_CLI_ROOT = Path(__file__).resolve().parent.parent


def _handle_collect_lines(argv):
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


def _handle_props_scan(argv):
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


def _handle_line_history(argv):
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


def _handle_clv_eval(argv):
    store    = LineStore()
    date_str = datetime.now().strftime("%Y-%m-%d")

    for tok in argv[2:]:
        if _looks_like_date(tok):
            date_str = tok
            break

    return store.clv_summary_for_date(date_str)


def _handle_stale_lines(argv):
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


def _handle_injury_alerts(argv):
    store    = LineStore()
    date_str = datetime.now().strftime("%Y-%m-%d")

    for tok in argv[2:]:
        if _looks_like_date(tok):
            date_str = tok
            break

    alerts = store.get_alerts(date_str)
    return {
        "success":    True,
        "date":       date_str,
        "alertCount": len(alerts),
        "alerts":     alerts,
    }


_COMMANDS = {
    "collect_lines":  _handle_collect_lines,
    "props_scan":     _handle_props_scan,
    "line_history":   _handle_line_history,
    "clv_eval":       _handle_clv_eval,
    "stale_lines":    _handle_stale_lines,
    "injury_alerts":  _handle_injury_alerts,
}


def handle_line_command(command, argv):  # shim — router no longer calls this
    h = _COMMANDS.get(command)
    return h(argv) if h else None
