#!/usr/bin/env python3
"""
Injury event monitor.

Polls injury feeds for teams playing today, diffs against the last known
player-status snapshot, and generates ranked alerts for newly injured
players' teammates.

Usage:
  .venv/Scripts/python.exe scripts/injury_monitor.py               # run once
  .venv/Scripts/python.exe scripts/injury_monitor.py --interval 15 # poll every 15 min
  .venv/Scripts/python.exe scripts/injury_monitor.py --teams MIA,BOS  # specific teams only
  .venv/Scripts/python.exe scripts/injury_monitor.py --json        # machine-readable output

On each triggered injury event:
  1. Identifies affected teammates on the same team playing today.
  2. Re-runs compute_projection() for each teammate.
  3. Compares projection against current lines from LineStore (or direct API fallback).
  4. Writes ranked alerts to data/alerts/YYYY-MM-DD.jsonl.
  5. Prints top opportunities to stdout.

Alert schema written to data/alerts/YYYY-MM-DD.jsonl:
  alert_id, generated_at, reason_type="injury",
  injured_player_name, injured_player_id, injured_team_abbr, new_status, confidence,
  affected_players: [{
    player_name, player_id, stat, model_projection,
    current_lines: [{book, line, over_odds, under_odds}],
    edge_pct, recommended_side, confidence
  }]
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT, ".env"), override=True)

from core import nba_data_collection as dc
from core.nba_injury_news import fetch_nba_injury_news
from core.nba_line_store import LineStore, _normalize_name, _utc_now_iso

# Stats to re-project for affected teammates
_REPROJECT_STATS = ["pts", "reb", "ast", "fg3m", "tov"]
# Min signal confidence to trigger an alert
_MIN_CONF = 0.60
# Min model edge to include an opportunity in an alert
_MIN_EDGE = 0.03


def _get_todays_team_abbrs():
    """Return set of team abbreviations playing today."""
    result = dc.get_todays_games()
    abbrs  = set()
    for g in result.get("games", []):
        abbrs.add(g.get("homeTeam", {}).get("abbreviation", ""))
        abbrs.add(g.get("awayTeam", {}).get("abbreviation", ""))
    return {a for a in abbrs if a}


def _get_game_context_for_team(team_abbr):
    """
    Return (opponent_abbr, is_home) for a given team from today's schedule,
    or (None, None) if not found.
    """
    result = dc.get_todays_games()
    for g in result.get("games", []):
        home_abbr = g.get("homeTeam", {}).get("abbreviation", "")
        away_abbr = g.get("awayTeam", {}).get("abbreviation", "")
        if home_abbr == team_abbr:
            return away_abbr, True
        if away_abbr == team_abbr:
            return home_abbr, False
    return None, None


def _get_team_roster_player_ids(team_abbr):
    """Return list of active player dicts for a team."""
    result = dc.get_team_roster_status(team_abbr)
    if not result.get("success"):
        return []
    # Only include players who have actually been playing recently
    return [
        p for p in result.get("players", [])
        if p.get("status") == "Active"
    ]


def _get_current_line(store: LineStore, player_name: str, stat: str):
    """
    Try LineStore first, then fall back to direct Odds API if no stored lines.
    Returns list of {book, line, over_odds, under_odds}.
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    snaps    = store.get_snapshots(date_str, stat=stat, player_name=player_name)
    if snaps:
        # Return latest snapshot per book
        latest = {}
        for s in sorted(snaps, key=lambda x: x.get("timestamp_utc", "")):
            latest[s.get("book", "")] = s
        return [
            {
                "book":       s.get("book"),
                "line":       s.get("line"),
                "over_odds":  s.get("over_odds"),
                "under_odds": s.get("under_odds"),
                "source":     "line_store",
            }
            for s in latest.values()
        ]
    return []


def _compute_edge(projection: float, line: float, stdev: float, side: str) -> float:
    """Simple normal-distribution edge estimate."""
    if not line or not stdev or stdev <= 0:
        return 0.0
    try:
        import math
        z = (projection - line) / stdev
        if side == "under":
            z = -z
        # Approximate P(over|under) via standard normal CDF
        p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        # Edge vs -110 breakeven (52.38%)
        return round(p - 0.5238, 4)
    except Exception:
        return 0.0


def _run_injury_alert(store: LineStore, signal: dict, dry_run: bool = False) -> dict:
    """
    Given a triggered injury signal, re-project affected teammates and
    return a structured alert dict.
    """
    from core.nba_data_prep import compute_projection

    injured_name   = signal.get("playerName", "unknown")
    injured_id     = signal.get("playerId")
    team_abbr      = signal.get("teamAbbr", "")
    new_status     = signal.get("status", "")
    confidence     = signal.get("confidence", 0.0)

    opponent_abbr, is_home = _get_game_context_for_team(team_abbr)
    if opponent_abbr is None:
        return {
            "success": False,
            "error":   f"{team_abbr} not playing today",
            "injured_player_name": injured_name,
        }

    # Get teammates
    roster = _get_team_roster_player_ids(team_abbr)
    teammates = [
        p for p in roster
        if int(p.get("playerId", 0) or 0) != int(injured_id or 0)
    ]

    affected = []
    for player in teammates:
        pid        = int(player.get("playerId", 0) or 0)
        pname      = player.get("name", "") or player.get("playerName", "")
        if not pid or not pname:
            continue

        try:
            proj_result = compute_projection(
                player_id=pid,
                opponent_abbr=opponent_abbr,
                is_home=bool(is_home),
            )
        except Exception:
            continue

        if not proj_result.get("success"):
            continue

        projections = proj_result.get("projections", {})
        for stat in _REPROJECT_STATS:
            proj_stat = projections.get(stat)
            if not proj_stat:
                continue
            projection = float(proj_stat.get("projection") or 0)
            stdev      = float(proj_stat.get("projStdev") or proj_stat.get("stdev") or 1)
            if projection <= 0:
                continue

            lines = _get_current_line(store, pname, stat)
            if not lines:
                continue

            for line_info in lines:
                line = line_info.get("line")
                if not line:
                    continue
                over_edge  = _compute_edge(projection, line, stdev, "over")
                under_edge = _compute_edge(projection, line, stdev, "under")
                best_edge  = max(over_edge, under_edge)
                best_side  = "over" if over_edge >= under_edge else "under"

                if best_edge < _MIN_EDGE:
                    continue

                conf_label = (
                    "high"   if best_edge >= 0.07 else
                    "medium" if best_edge >= 0.04 else
                    "low"
                )
                affected.append({
                    "player_name":      pname,
                    "player_id":        pid,
                    "stat":             stat,
                    "model_projection": round(projection, 2),
                    "stdev":            round(stdev, 2),
                    "book":             line_info.get("book"),
                    "line":             line,
                    "over_odds":        line_info.get("over_odds"),
                    "under_odds":       line_info.get("under_odds"),
                    "edge_pct":         round(best_edge * 100, 2),
                    "recommended_side": best_side,
                    "confidence":       conf_label,
                    "reason_type":      "injury",
                })

    affected.sort(key=lambda x: -x["edge_pct"])

    alert = {
        "alert_id":             str(uuid.uuid4()),
        "generated_at":         _utc_now_iso(),
        "reason_type":          "injury",
        "injured_player_name":  injured_name,
        "injured_player_id":    injured_id,
        "injured_team_abbr":    team_abbr,
        "opponent_abbr":        opponent_abbr,
        "is_home":              is_home,
        "new_status":           new_status,
        "signal_confidence":    confidence,
        "source":               signal.get("source", ""),
        "affected_count":       len(affected),
        "affected_players":     affected,
    }

    if not dry_run:
        store.append_alert(alert)

    return {"success": True, "alert": alert}


def _run_once(store: LineStore, teams_override=None, dry_run: bool = False) -> dict:
    """Single monitor pass: diff injury status and fire alerts for new events."""
    today_abbrs = _get_todays_team_abbrs()
    if not today_abbrs:
        return {"success": True, "message": "no games today", "triggeredCount": 0}

    teams_to_check = (
        {a.upper() for a in teams_override.split(",")} & today_abbrs
        if teams_override
        else today_abbrs
    )

    # Load previous status
    prev_map = store.load_injury_status()
    new_signals_all = []

    for team_abbr in sorted(teams_to_check):
        try:
            result = fetch_nba_injury_news(team_abbr, lookback_hours=4)
            if not result.get("success"):
                continue
            for sig in result.get("signals", []):
                sig["teamAbbr"] = team_abbr
                new_signals_all.append(sig)
        except Exception:
            continue

    # Diff against previous state
    triggered = store.diff_injury_status(new_signals_all, prev_map)

    # Update rolling status map
    updated_map = dict(prev_map)
    for sig in new_signals_all:
        key = _normalize_name(sig.get("playerName", ""))
        if not key:
            continue
        prev_entry = updated_map.get(key, {})
        # Only update if confidence improved or status escalated
        if sig.get("confidence", 0) >= prev_entry.get("confidence", 0):
            updated_map[key] = {
                "status":        sig.get("status"),
                "confidence":    sig.get("confidence"),
                "team_abbr":     sig.get("teamAbbr"),
                "last_seen_utc": _utc_now_iso(),
            }
    store.save_injury_status(updated_map)

    # Fire alerts for triggered events
    alerts_generated = []
    for sig in triggered:
        if float(sig.get("confidence", 0)) < _MIN_CONF:
            continue
        result = _run_injury_alert(store, sig, dry_run=dry_run)
        if result.get("success") and result.get("alert"):
            alerts_generated.append(result["alert"])

    return {
        "success":          True,
        "teamsChecked":     len(teams_to_check),
        "signalsFound":     len(new_signals_all),
        "triggeredEvents":  len(triggered),
        "alertsGenerated":  len(alerts_generated),
        "alerts":           alerts_generated,
    }


def main():
    parser = argparse.ArgumentParser(description="NBA injury event monitor")
    parser.add_argument("--interval", type=int,   default=0,
                        help="Polling interval in minutes (0 = run once)")
    parser.add_argument("--teams",    type=str,   default=None,
                        help="Comma-separated team abbreviations (default: all today)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Do not write alerts to disk")
    parser.add_argument("--json",     action="store_true",
                        help="Output machine-readable JSON to stdout")
    args = parser.parse_args()

    store = LineStore()

    def _run_and_print():
        summary = _run_once(store, teams_override=args.teams, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(summary, default=str), flush=True)
        else:
            _print_summary(summary)
        return summary

    if args.interval <= 0:
        s = _run_and_print()
        sys.exit(0 if s.get("success") else 1)

    poll = 0
    while True:
        poll += 1
        print(f"\n[injury monitor poll #{poll}] {_utc_now_iso()}", flush=True)
        _run_and_print()
        print(f"  sleeping {args.interval}m...", flush=True)
        time.sleep(args.interval * 60)


def _print_summary(s: dict) -> None:
    if not s.get("success"):
        print(f"  ERROR: {s.get('error')}", flush=True)
        return
    if s.get("message"):
        print(f"  {s['message']}", flush=True)
        return

    print(
        f"  teams={s['teamsChecked']}  signals={s['signalsFound']}  "
        f"triggered={s['triggeredEvents']}  alerts={s['alertsGenerated']}",
        flush=True,
    )

    for alert in s.get("alerts", []):
        print(
            f"\n  [INJURY ALERT] {alert['injured_player_name']} ({alert['injured_team_abbr']}) "
            f"→ {alert['new_status']}  conf={alert['signal_confidence']:.0%}",
            flush=True,
        )
        for opp in alert.get("affected_players", [])[:5]:
            print(
                f"    {opp['player_name']:<28} {opp['stat']:<5} "
                f"{opp['recommended_side'].upper():<6} "
                f"proj={opp['model_projection']:.1f}  line={opp['line']}  "
                f"edge={opp['edge_pct']:+.1f}%  [{opp['confidence']}]",
                flush=True,
            )


if __name__ == "__main__":
    main()
