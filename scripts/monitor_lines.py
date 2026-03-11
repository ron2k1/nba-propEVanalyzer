#!/usr/bin/env python3
"""
Line movement monitor: detect significant line moves and alert via Discord.

Compares current line snapshots against previous snapshots for today's
best_today signals. Alerts when a line moves >= threshold (default 1.0 point)
or when odds shift >= 15 cents.

Designed to run every 2 hours via Windows Task Scheduler (aligned with
NBASnapshotCollection).

Usage
-----
.venv/Scripts/python.exe scripts/monitor_lines.py
.venv/Scripts/python.exe scripts/monitor_lines.py --threshold 0.5
.venv/Scripts/python.exe scripts/monitor_lines.py --dry-run
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

_log = logging.getLogger("monitor_lines")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)

PREV_STATE_PATH = ROOT / "data" / "logs" / "line_monitor_state.json"


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_prev_state() -> dict:
    if PREV_STATE_PATH.exists():
        try:
            return json.loads(PREV_STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state: dict):
    PREV_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREV_STATE_PATH.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")


def _get_current_lines() -> dict:
    """
    Fetch today's best_today signals and extract their line/odds data.
    Returns {player_stat_key: {player, stat, line, overOdds, underOdds, book, ...}}.
    """
    from core.nba_bet_tracking import best_today

    try:
        result = best_today(limit=30)
    except Exception as exc:
        _log.warning("best_today_props failed: %s", exc)
        return {}

    if not result.get("success"):
        return {}

    current = {}
    for play in result.get("policyQualified", []) + result.get("modelLeans", []):
        player = play.get("playerName", "")
        stat = play.get("stat", "")
        if not player or not stat:
            continue

        key = f"{player}|{stat}"
        line = play.get("line")
        over_odds = play.get("bestOverOdds") or play.get("overOdds")
        under_odds = play.get("bestUnderOdds") or play.get("underOdds")
        over_book = play.get("bestOverBook", "")
        under_book = play.get("bestUnderBook", "")
        side = play.get("recommendedSide", "")
        ev = play.get("recommendedEvPct", 0)

        current[key] = {
            "player": player,
            "stat": stat,
            "line": line,
            "overOdds": over_odds,
            "underOdds": under_odds,
            "overBook": over_book,
            "underBook": under_book,
            "side": side,
            "evPct": ev,
            "ts": _utc_now_iso(),
        }

    return current


def _detect_movements(
    prev: dict, current: dict, line_threshold: float = 1.0, odds_threshold: int = 15,
) -> list[dict]:
    """
    Compare previous and current lines, return list of significant movements.
    """
    movements = []

    for key, cur in current.items():
        if key not in prev:
            continue  # new signal, no comparison

        prv = prev[key]
        player = cur["player"]
        stat = cur["stat"]

        # Line movement
        prev_line = prv.get("line")
        cur_line = cur.get("line")
        if prev_line is not None and cur_line is not None:
            delta = cur_line - prev_line
            if abs(delta) >= line_threshold:
                direction = "UP" if delta > 0 else "DOWN"
                movements.append({
                    "type": "line_move",
                    "player": player,
                    "stat": stat,
                    "prevLine": prev_line,
                    "curLine": cur_line,
                    "delta": round(delta, 1),
                    "direction": direction,
                    "side": cur.get("side", ""),
                    "evPct": cur.get("evPct", 0),
                })

        # Odds shift (compare absolute american odds difference)
        for odds_key, label in [("overOdds", "OVER"), ("underOdds", "UNDER")]:
            prev_odds = prv.get(odds_key)
            cur_odds = cur.get(odds_key)
            if prev_odds is not None and cur_odds is not None:
                try:
                    odds_delta = abs(int(cur_odds) - int(prev_odds))
                    if odds_delta >= odds_threshold:
                        movements.append({
                            "type": "odds_shift",
                            "player": player,
                            "stat": stat,
                            "side": label,
                            "prevOdds": prev_odds,
                            "curOdds": cur_odds,
                            "delta": odds_delta,
                        })
                except (ValueError, TypeError):
                    pass

    return movements


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Line movement monitor + Discord alert")
    parser.add_argument("--threshold", type=float, default=1.0,
                        help="Minimum line move to alert (default: 1.0 point)")
    parser.add_argument("--odds-threshold", type=int, default=15,
                        help="Minimum odds shift to alert (default: 15 cents)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from scripts.ops_events import emit

    emit("line_monitor", "run_started", {})

    # Load previous state
    prev_state = _load_prev_state()
    prev_lines = prev_state.get("lines", {})

    # Get current lines
    current_lines = _get_current_lines()
    if not current_lines:
        result = {"success": True, "signals": 0, "movements": [], "message": "No signals to monitor"}
        emit("line_monitor", "run_succeeded", {"movements": 0})
        print(json.dumps(result))
        return 0

    # Detect movements
    movements = _detect_movements(
        prev_lines, current_lines,
        line_threshold=args.threshold,
        odds_threshold=args.odds_threshold,
    )

    # Save current state for next run
    if not args.dry_run:
        _save_state({"lines": current_lines, "ts": _utc_now_iso()})

    if args.dry_run:
        result = {
            "success": True, "dryRun": True,
            "signals": len(current_lines), "movements": movements,
        }
        print(json.dumps(result, indent=2, default=str))
        return 0

    # Send Discord alert if movements found
    discord_result = None
    if movements:
        try:
            from scripts.discord_notify import notify_line_movement
            discord_result = notify_line_movement(movements)
        except Exception as exc:
            discord_result = {"success": False, "error": str(exc)}
            _log.warning("Discord alert failed (non-fatal): %s", exc)

    result = {
        "success": True,
        "signals": len(current_lines),
        "movements": movements,
        "discord": discord_result,
    }
    emit("line_monitor", "run_succeeded", {"movements": len(movements)})
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
