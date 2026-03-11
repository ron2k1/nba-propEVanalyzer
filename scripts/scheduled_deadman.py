#!/usr/bin/env python3
"""
Scheduled dead-man check: poll ops health and alert via Discord if stale.

Designed to run every 4 hours via Windows Task Scheduler. If any scheduled
task hasn't succeeded within its expected interval, sends an orange Discord
alert to the webhook channel.

Usage
-----
.venv/Scripts/python.exe scripts/scheduled_deadman.py
.venv/Scripts/python.exe scripts/scheduled_deadman.py --dry-run
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

from scripts.ops_events import read_ops_health, emit


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Dead-man health check + Discord alert")
    parser.add_argument("--dry-run", action="store_true", help="Print health without sending alert")
    args = parser.parse_args()

    health = read_ops_health()
    stale_tasks = {k: v for k, v in health.get("tasks", {}).items() if v.get("stale")}

    if args.dry_run:
        result = {"success": True, "dryRun": True, "healthy": health["healthy"],
                  "staleTasks": list(stale_tasks.keys())}
        print(json.dumps(result, indent=2))
        return 0

    emit("deadman_check", "run_started", {"taskCount": len(health.get("tasks", {}))})

    if not stale_tasks:
        result = {"success": True, "healthy": True, "staleTasks": []}
        emit("deadman_check", "run_succeeded", {"healthy": True})
        print(json.dumps(result))
        return 0

    # Stale tasks found — send Discord alert
    try:
        from scripts.discord_notify import notify_deadman
        discord_result = notify_deadman(health)
    except Exception as exc:
        discord_result = {"success": False, "error": str(exc)}
        print(f"Discord alert failed (non-fatal): {exc}", file=sys.stderr)

    result = {
        "success": True,
        "healthy": False,
        "staleTasks": list(stale_tasks.keys()),
        "discord": discord_result,
    }
    emit("deadman_check", "run_succeeded", {
        "healthy": False, "staleTasks": list(stale_tasks.keys()),
    })
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
