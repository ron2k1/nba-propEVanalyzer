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
from scripts.ops_events import emit, retry_transient, classify_error

DEFAULT_LOG_PATH = ROOT / "data" / "logs" / "scheduled_runs.jsonl"

RUN_TYPE = "morning_settle"


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
    try:
        result = retry_transient(
            lambda: JOURNAL_COMMANDS[name](argv),
            max_retries=2,
            base_delay=5.0,
            task_name=RUN_TYPE,
            step_name=name,
        )
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 3)
        return {
            "name": name,
            "argv": argv[1:],
            "success": False,
            "durationSec": elapsed,
            "error": str(exc),
            "errorClass": classify_error(exc),
            "result": {"success": False, "error": str(exc)},
        }
    elapsed = round(time.perf_counter() - started, 3)
    step_result = {
        "name": name,
        "argv": argv[1:],
        "success": bool((result or {}).get("success", False)),
        "durationSec": elapsed,
        "result": result,
    }
    if step_result["success"]:
        emit(RUN_TYPE, "step_completed", {"step": name, "durationSec": elapsed})
    return step_result


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
            "runType": RUN_TYPE,
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

    emit(RUN_TYPE, "run_started", {"steps": ["paper_settle", "paper_summary"]})

    step_results = [
        _run_step("paper_settle", steps[0]),
        _run_step("paper_summary", steps[1]),
    ]
    ok = all(step["success"] for step in step_results)
    duration = round(time.perf_counter() - run_started, 3)
    payload = {
        "success": ok,
        "dryRun": False,
        "runType": RUN_TYPE,
        "createdAtUtc": started_at,
        "finishedAtUtc": _utc_now_iso(),
        "durationSec": duration,
        "steps": step_results,
    }
    _append_jsonl(log_path, payload)

    if ok:
        emit(RUN_TYPE, "run_succeeded", {"durationSec": duration})
    else:
        failed_step = next((s for s in step_results if not s["success"]), {})
        emit(RUN_TYPE, "run_failed", {
            "durationSec": duration,
            "failedStep": failed_step.get("name", ""),
            "error": failed_step.get("error", ""),
            "errorClass": failed_step.get("errorClass", classify_error(
                failed_step.get("error", "")
            )),
        })

    print(json.dumps(payload, indent=2))

    # Discord notifications
    try:
        from scripts.discord_notify import notify_morning_summary, notify_failure
        if ok:
            notify_morning_summary(payload)
        else:
            failed_step = next((s for s in step_results if not s["success"]), {})
            notify_failure(
                RUN_TYPE,
                failed_step.get("error", "unknown error"),
                failed_step.get("errorClass", "unknown"),
            )
    except Exception as exc:
        print(f"Discord notify failed (non-fatal): {exc}", file=sys.stderr)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
