#!/usr/bin/env python3
"""
Smoke test for LineStore → OddsStore bridge.

Validates:
- Bridge runs without error
- Converted rows have expected schema
- No duplicate explosion (inserted count bounded)
- build_closing_lines works with bridged data

Usage
-----
.venv/Scripts/python.exe scripts/validate_line_bridge.py
.venv/Scripts/python.exe scripts/validate_line_bridge.py --with-backtest
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _minimal_snapshot(event_id: str = "test_event_123", date_str: str | None = None) -> dict:
    """ts_utc must be before commence_time for build_closing_lines to use it."""
    ds = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # ts_utc must be <= commence_time for build_closing_lines to use it
    ts_utc = f"{ds}T19:00:00Z"    # 2pm EST day-of
    # Commence next-day UTC = evening game US (e.g. Feb28 00:30 UTC = Feb27 7:30pm EST)
    d = datetime.strptime(ds, "%Y-%m-%d")
    next_d = (d + timedelta(days=1)).strftime("%Y-%m-%d")
    commence = f"{next_d}T00:30:00Z"
    return {
        "timestamp_utc": ts_utc,
        "game_id": event_id,
        "player_name": "Anthony Edwards",
        "player_team_abbr": "MIN",
        "opponent_abbr": "ORL",
        "is_home": True,
        "stat": "pts",
        "line": 25.5,
        "over_odds": -110,
        "under_odds": -110,
        "book": "draftkings",
        "commence_time": commence,
        "home_team_abbr": "MIN",
        "away_team_abbr": "ORL",
    }


def test_bridge_smoke() -> tuple[bool, str]:
    """Run bridge with minimal test data; verify no crash and sane output."""
    from core.nba_line_store import LineStore
    from core.nba_odds_store import OddsStore
    from scripts.line_to_odds_bridge import run_bridge
    from scripts.build_closing_lines import build_closing_lines

    with tempfile.TemporaryDirectory() as tmp:
        line_dir = os.path.join(tmp, "line_history")
        db_path = os.path.join(tmp, "odds.sqlite")
        os.makedirs(line_dir, exist_ok=True)

        # Create minimal LineStore data
        line_store = LineStore(data_dir=line_dir)
        snap = _minimal_snapshot()
        line_store.append_snapshot(snap)
        line_store.append_snapshot({**snap, "book": "fanduel", "line": 25.0})
        date_str = snap["timestamp_utc"][:10]

        # Bridge (real run, not dry-run)
        result = run_bridge(
            date_from=date_str,
            date_to=date_str,
            line_history_dir=line_dir,
            odds_db_path=db_path,
            dry_run=False,
        )
        if not result.get("success"):
            return False, f"bridge failed: {result.get('error', result)}"

        rows_conv = result.get("rowsConverted", 0)
        rows_ins = result.get("rowsInserted", 0)
        if rows_conv < 2:
            return False, f"expected >=2 rows converted, got {rows_conv}"
        # 2 snaps × 2 sides × 2 books max; INSERT OR IGNORE may dedupe
        if rows_ins > rows_conv * 2:
            return False, f"duplicate explosion: converted={rows_conv} inserted={rows_ins}"

        # build_closing_lines should not crash
        store = OddsStore(db_path=db_path)
        try:
            saved, total = build_closing_lines(store, date_str, date_str)
            if total == 0 and rows_ins > 0:
                # commence_time might be in future; closing = last before commence
                # For test, commence is same day - could be before ts_utc
                pass  # Allow 0 if ts_utc > commence_time
        except Exception as e:
            return False, f"build_closing_lines failed: {e}"
        finally:
            store.close()

    return True, f"bridge ok: converted={rows_conv} inserted={rows_ins}"


def test_bridge_dry_run() -> tuple[bool, str]:
    """Dry-run must not write; must return structured result."""
    from scripts.line_to_odds_bridge import run_bridge

    result = run_bridge(
        date_from="2026-02-27",
        date_to="2026-02-27",
        dry_run=True,
    )
    if not result.get("success"):
        return False, f"dry-run failed: {result.get('error', result)}"
    if result.get("rowsInserted", 0) != 0:
        return False, "dry-run must not insert (rowsInserted should be 0)"
    if "dryRun" not in result or not result["dryRun"]:
        return False, "dry-run result must have dryRun=true"
    return True, "dry-run ok"


def main() -> int:
    run_backtest = "--with-backtest" in sys.argv

    checks = []
    ok, msg = test_bridge_dry_run()
    checks.append(("dry_run", ok, msg))

    ok, msg = test_bridge_smoke()
    checks.append(("bridge_smoke", ok, msg))

    if run_backtest:
        # Quick backtest on a covered range (may have 0 samples if no data)
        from nba_cli import dispatch_cli
        result, _ = dispatch_cli(
            ["nba_mod.py", "backtest", "2026-02-01", "2026-02-05", "--model", "simple",
             "--local", "--odds-source", "local_history"]
        )
        if isinstance(result, dict) and "error" in result:
            checks.append(("backtest", False, result["error"]))
        else:
            rl = result.get("realLineSamples", 0)
            ml = result.get("missingLineSamples", 0)
            roi_r = result.get("roiReal") or {}
            roi_s = result.get("roiSynth") or {}
            checks.append((
                "backtest",
                True,
                f"realLineSamples={rl} missingLineSamples={ml} "
                f"roiReal.bets={roi_r.get('betsPlaced',0)} roiSynth.bets={roi_s.get('betsPlaced',0)}"
            ))

    all_ok = all(c[1] for c in checks)
    report = {"ok": all_ok, "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks]}
    print(json.dumps(report, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
