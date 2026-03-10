#!/usr/bin/env python3
"""Scheduler-safe wrapper for line collection and the daily signal pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nba_cli.line_commands import _COMMANDS as LINE_COMMANDS
from nba_cli.scan_commands import _COMMANDS as SCAN_COMMANDS
from nba_cli.tracking_commands import _COMMANDS as TRACKING_COMMANDS

DEFAULT_LOG_PATH = ROOT / "data" / "logs" / "scheduled_runs.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        handle.write("\n")


def _run_step(name: str, handler, argv: list[str]) -> dict:
    started = time.perf_counter()
    result = handler(argv)
    elapsed = round(time.perf_counter() - started, 3)
    return {
        "name": name,
        "argv": argv[1:],
        "success": bool((result or {}).get("success", False)),
        "durationSec": elapsed,
        "result": result,
    }


def _build_pipeline_plan(args) -> list[dict]:
    plan = [
        {
            "name": "collect_lines",
            "argv": [
                "nba_mod.py",
                "collect_lines",
                "--books",
                args.books,
                "--stats",
                args.stats,
            ],
            "handler": LINE_COMMANDS["collect_lines"],
        }
    ]
    if args.collect_only:
        return plan
    sweep_argv = ["nba_mod.py", "roster_sweep"]
    if args.date:
        sweep_argv.append(args.date)
    plan.append(
        {
            "name": "roster_sweep",
            "argv": sweep_argv,
            "handler": SCAN_COMMANDS["roster_sweep"],
        }
    )
    plan.append(
        {
            "name": "best_today",
            "argv": ["nba_mod.py", "best_today", str(args.limit)],
            "handler": TRACKING_COMMANDS["best_today"],
        }
    )
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Run scheduled NBA prop pipeline tasks.")
    parser.add_argument("--books", default="betmgm,draftkings,fanduel,pinnacle")
    parser.add_argument("--stats", default="pts,ast,reb,pra,fg3m,stl,blk")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--date", default=None, help="Optional YYYY-MM-DD for roster_sweep.")
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    args = parser.parse_args()

    run_type = "collect_only" if args.collect_only else "full_pipeline"
    planned_steps = _build_pipeline_plan(args)
    log_path = Path(args.log_path).expanduser().resolve()

    if args.dry_run:
        payload = {
            "success": True,
            "dryRun": True,
            "runType": run_type,
            "createdAtUtc": _utc_now_iso(),
            "steps": [{"name": step["name"], "argv": step["argv"][1:]} for step in planned_steps],
        }
        print(json.dumps(payload, indent=2))
        return 0

    started_at = _utc_now_iso()
    run_started = time.perf_counter()
    step_results = []
    ok = True

    for step in planned_steps:
        step_result = _run_step(step["name"], step["handler"], step["argv"])
        step_results.append(step_result)
        if not step_result["success"]:
            ok = False
            break

    payload = {
        "success": ok,
        "dryRun": False,
        "runType": run_type,
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
