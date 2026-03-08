#!/usr/bin/env python3
"""Scheduler-safe wrapper for morning settlement and validation summary."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nba_cli.journal_commands import _COMMANDS as JOURNAL_COMMANDS

DEFAULT_LOG_PATH = ROOT / "data" / "logs" / "scheduled_runs.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_settle_date() -> str:
    return (datetime.now().date() - timedelta(days=1)).isoformat()


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        handle.write("\n")


def _run_step(name: str, argv: list[str]) -> dict:
    started = time.perf_counter()
    result = JOURNAL_COMMANDS[name](argv)
    elapsed = round(time.perf_counter() - started, 3)
    return {
        "name": name,
        "argv": argv[1:],
        "success": bool((result or {}).get("success", False)),
        "durationSec": elapsed,
        "result": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run scheduled settlement and paper summary.")
    parser.add_argument("--date", default=_default_settle_date(), help="Settlement date YYYY-MM-DD.")
    parser.add_argument("--window-days", type=int, default=14)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    args = parser.parse_args()

    steps = [
        ["nba_mod.py", "paper_settle", args.date],
        ["nba_mod.py", "paper_summary", "--window-days", str(args.window_days)],
    ]
    if args.dry_run:
        payload = {
            "success": True,
            "dryRun": True,
            "runType": "morning_settle",
            "createdAtUtc": _utc_now_iso(),
            "steps": [
                {"name": "paper_settle", "argv": steps[0][1:]},
                {"name": "paper_summary", "argv": steps[1][1:]},
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    log_path = Path(args.log_path).expanduser().resolve()
    started_at = _utc_now_iso()
    run_started = time.perf_counter()
    step_results = [
        _run_step("paper_settle", steps[0]),
        _run_step("paper_summary", steps[1]),
    ]
    ok = all(step["success"] for step in step_results)
    payload = {
        "success": ok,
        "dryRun": False,
        "runType": "morning_settle",
        "createdAtUtc": started_at,
        "finishedAtUtc": _utc_now_iso(),
        "durationSec": round(time.perf_counter() - run_started, 3),
        "steps": step_results,
    }
    _append_jsonl(log_path, payload)
    print(json.dumps(payload, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
