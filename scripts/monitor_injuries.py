#!/usr/bin/env python3
"""
Injury monitor: poll NBA injury news and alert via Discord for high-impact changes.

Checks for new injury signals (Out/Doubtful/Questionable) for teams playing
today. Alerts when a new high-confidence signal appears that wasn't in the
previous check.

Designed to run every 2 hours on game days via Windows Task Scheduler.

Usage
-----
.venv/Scripts/python.exe scripts/monitor_injuries.py
.venv/Scripts/python.exe scripts/monitor_injuries.py --min-confidence 0.70
.venv/Scripts/python.exe scripts/monitor_injuries.py --dry-run
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

_log = logging.getLogger("monitor_injuries")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)

PREV_STATE_PATH = ROOT / "data" / "logs" / "injury_monitor_state.json"


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


def _get_todays_teams() -> list[str]:
    """Get team abbreviations for today's games."""
    try:
        from core.nba_data_collection import get_todays_games
        result = get_todays_games()
        if not result.get("success"):
            return []
        teams = []
        for game in result.get("games", []):
            home = game.get("homeTeam", {}).get("abbreviation", "")
            away = game.get("awayTeam", {}).get("abbreviation", "")
            if home:
                teams.append(home)
            if away:
                teams.append(away)
        return teams
    except Exception as exc:
        _log.warning("Failed to get today's games: %s", exc)
        return []


def _fetch_injuries_for_teams(teams: list[str], lookback_hours: int = 12) -> list[dict]:
    """Fetch injury signals for all given teams."""
    try:
        from core.nba_injury_news import fetch_nba_injury_news
    except ImportError:
        _log.error("nba_injury_news module not available")
        return []

    all_signals = []
    for team in teams:
        try:
            result = fetch_nba_injury_news(team, lookback_hours=lookback_hours)
            if result.get("success"):
                for sig in result.get("signals", []):
                    sig["team"] = team
                    all_signals.append(sig)
        except Exception as exc:
            _log.warning("Injury fetch failed for %s: %s", team, exc)

    return all_signals


def _filter_new_signals(
    prev_keys: set, signals: list[dict], min_confidence: float = 0.60,
) -> list[dict]:
    """Filter to only new, high-confidence signals not seen in previous check."""
    new_signals = []
    for sig in signals:
        # Build a dedup key from player + status
        player = sig.get("player", "")
        status = sig.get("status", "")
        key = f"{player}|{status}"

        confidence = sig.get("confidence", 0)
        if confidence >= min_confidence and key not in prev_keys:
            new_signals.append(sig)

    return new_signals


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Injury monitor + Discord alert")
    parser.add_argument("--min-confidence", type=float, default=0.60,
                        help="Minimum confidence to alert (default: 0.60)")
    parser.add_argument("--lookback-hours", type=int, default=12,
                        help="Hours to look back for news (default: 12)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from scripts.ops_events import emit

    emit("injury_monitor", "run_started", {})

    # Get today's teams
    teams = _get_todays_teams()
    if not teams:
        result = {"success": True, "teams": 0, "message": "No games today"}
        emit("injury_monitor", "run_succeeded", {"teams": 0, "newSignals": 0})
        print(json.dumps(result))
        return 0

    _log.info("Checking injuries for %d teams: %s", len(teams), ", ".join(teams))

    # Fetch injury signals
    all_signals = _fetch_injuries_for_teams(teams, lookback_hours=args.lookback_hours)

    # Load previous state to find NEW signals
    prev_state = _load_prev_state()
    prev_keys = set(prev_state.get("signalKeys", []))

    new_signals = _filter_new_signals(prev_keys, all_signals, min_confidence=args.min_confidence)

    # Build current signal keys for state persistence
    current_keys = set()
    for sig in all_signals:
        player = sig.get("player", "")
        status = sig.get("status", "")
        current_keys.add(f"{player}|{status}")

    # Save state
    if not args.dry_run:
        _save_state({
            "signalKeys": list(current_keys),
            "ts": _utc_now_iso(),
            "teams": teams,
        })

    if args.dry_run:
        result = {
            "success": True, "dryRun": True,
            "teams": len(teams), "totalSignals": len(all_signals),
            "newSignals": new_signals,
        }
        print(json.dumps(result, indent=2, default=str))
        return 0

    # Send Discord alert if new signals found
    discord_result = None
    if new_signals:
        try:
            from scripts.discord_notify import notify_injury_alert
            discord_result = notify_injury_alert(new_signals)
        except Exception as exc:
            discord_result = {"success": False, "error": str(exc)}
            _log.warning("Discord alert failed (non-fatal): %s", exc)

    result = {
        "success": True,
        "teams": len(teams),
        "totalSignals": len(all_signals),
        "newSignals": len(new_signals),
        "discord": discord_result,
    }
    emit("injury_monitor", "run_succeeded", {
        "teams": len(teams), "newSignals": len(new_signals),
    })
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
