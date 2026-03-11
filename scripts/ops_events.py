#!/usr/bin/env python3
"""
Ops event layer: structured event emission, error classification, and retry.

All scheduled scripts emit events through this module. Events are appended to
a single JSONL file (data/logs/scheduled_runs.jsonl) with a consistent schema.

Usage
-----
    from scripts.ops_events import emit, classify_error, retry_transient

    emit("full_pipeline", "run_started", {"steps": ["collect_lines", "roster_sweep"]})
    emit("full_pipeline", "run_succeeded", {"durationSec": 27.3})
    emit("full_pipeline", "run_failed", {"error": "HTTP 503", "errorClass": "transient"})
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_PATH = ROOT / "data" / "logs" / "scheduled_runs.jsonl"

_log = logging.getLogger("ops_events")


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

_TRANSIENT_PATTERNS = [
    "503",
    "502",
    "429",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "temporary failure",
    "rate limit",
    "service unavailable",
    "server error",
    "ConnectionError",
    "ReadTimeout",
]

_PERMANENT_PATTERNS = [
    "401",
    "403",
    "404",
    "invalid api key",
    "quota exceeded",
    "KeyError",
    "TypeError",
    "ValueError",
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",
]


def classify_error(error: str | Exception) -> str:
    """Classify an error as 'transient', 'permanent', or 'unknown'."""
    msg = str(error).lower()
    for pat in _TRANSIENT_PATTERNS:
        if pat.lower() in msg:
            return "transient"
    for pat in _PERMANENT_PATTERNS:
        if pat.lower() in msg:
            return "permanent"
    return "unknown"


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":"), default=str, ensure_ascii=False))
        f.write("\n")


def emit(
    task_name: str,
    event_type: str,
    data: dict[str, Any] | None = None,
    log_path: Path | str | None = None,
) -> dict:
    """
    Emit a structured ops event.

    Parameters
    ----------
    task_name : e.g. "full_pipeline", "morning_settle", "dense_collector"
    event_type : e.g. "run_started", "run_succeeded", "run_failed", "step_completed"
    data : arbitrary payload dict
    log_path : override log file path (default: data/logs/scheduled_runs.jsonl)

    Returns the emitted event dict.
    """
    event = {
        "ts": _utc_now_iso(),
        "task": task_name,
        "event": event_type,
        **(data or {}),
    }
    target = Path(log_path) if log_path else DEFAULT_LOG_PATH
    _append_jsonl(target, event)
    _log.debug("ops_event: %s/%s", task_name, event_type)
    return event


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def retry_transient(
    fn: Callable[[], Any],
    max_retries: int = 2,
    base_delay: float = 5.0,
    task_name: str = "",
    step_name: str = "",
) -> Any:
    """
    Retry fn() on transient errors with exponential backoff.
    Permanent and unknown errors are raised immediately.
    """
    last_exc = None
    for attempt in range(1 + max_retries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            error_class = classify_error(exc)
            if error_class != "transient" or attempt >= max_retries:
                emit(task_name, "retry_exhausted" if attempt > 0 else "step_failed", {
                    "step": step_name,
                    "error": str(exc),
                    "errorClass": error_class,
                    "attempt": attempt + 1,
                })
                raise
            delay = base_delay * (2 ** attempt)
            _log.warning(
                "%s/%s attempt %d failed (transient: %s), retrying in %.0fs",
                task_name, step_name, attempt + 1, exc, delay,
            )
            emit(task_name, "retry", {
                "step": step_name,
                "attempt": attempt + 1,
                "error": str(exc),
                "errorClass": "transient",
                "delaySec": delay,
            })
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Health query (used by /api/ops_health)
# ---------------------------------------------------------------------------

# Map of task names to their expected run intervals in hours
TASK_INTERVALS = {
    "full_pipeline": 24,
    "collect_only": 2,
    "morning_settle": 24,
    "dense_collector": 24,
    "bridge_and_build": 24,
    "deadman_check": 4,
    "line_monitor": 2,
    "injury_monitor": 2,
}

# Dead-man threshold multiplier (interval * this = stale)
STALE_MULTIPLIER = 1.25


def read_ops_health(log_path: Path | str | None = None) -> dict:
    """
    Parse scheduled_runs.jsonl and return per-task health status.

    Returns
    -------
    {
        "healthy": bool,
        "tasks": {
            "full_pipeline": {
                "lastSuccess": "2026-03-10T22:00:00Z" | null,
                "lastFailure": "2026-03-10T17:00:00Z" | null,
                "lastError": "..." | null,
                "lastErrorClass": "transient" | "permanent" | null,
                "runsLast24h": 5,
                "failuresLast24h": 1,
                "stale": false,
                "staleThresholdHours": 26,
            },
            ...
        },
        "checkedAtUtc": "...",
    }
    """
    target = Path(log_path) if log_path else DEFAULT_LOG_PATH

    # Per-task tracking
    task_data: dict[str, dict] = {}
    now = datetime.now(UTC)
    cutoff_24h = (now - __import__("datetime").timedelta(hours=24)).isoformat()

    if target.exists():
        try:
            with target.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Determine task name — handle both old and new event formats
                    task = entry.get("task") or entry.get("runType") or "unknown"
                    ts = entry.get("ts") or entry.get("createdAtUtc") or ""
                    success = entry.get("success")
                    event_type = entry.get("event", "")

                    if task not in task_data:
                        task_data[task] = {
                            "lastSuccess": None,
                            "lastFailure": None,
                            "lastError": None,
                            "lastErrorClass": None,
                            "runsLast24h": 0,
                            "failuresLast24h": 0,
                        }

                    td = task_data[task]

                    # Count runs in last 24h (skip non-run events like retries)
                    is_run = (
                        event_type in ("run_succeeded", "run_failed", "")
                        or success is not None
                    )
                    if is_run and ts >= cutoff_24h:
                        td["runsLast24h"] += 1

                    # Track success/failure
                    if success is True or event_type == "run_succeeded":
                        if not td["lastSuccess"] or ts > td["lastSuccess"]:
                            td["lastSuccess"] = ts
                    elif success is False or event_type == "run_failed":
                        if not td["lastFailure"] or ts > td["lastFailure"]:
                            td["lastFailure"] = ts
                        if ts >= cutoff_24h:
                            td["failuresLast24h"] += 1
                        # Extract error info
                        error_msg = entry.get("error") or ""
                        if not error_msg:
                            # Check steps for errors
                            for step in entry.get("steps", []):
                                if not step.get("success") and step.get("result"):
                                    error_msg = step["result"].get("error", "")
                                    if error_msg:
                                        break
                        if error_msg:
                            td["lastError"] = str(error_msg)[:200]
                            td["lastErrorClass"] = classify_error(error_msg)

        except OSError as exc:
            _log.warning("Could not read ops log: %s", exc)

    # Compute staleness
    all_healthy = True
    for task, td in task_data.items():
        interval_h = TASK_INTERVALS.get(task, 26)
        threshold_h = interval_h * STALE_MULTIPLIER
        td["staleThresholdHours"] = round(threshold_h, 1)

        if td["lastSuccess"]:
            try:
                last_ok = datetime.fromisoformat(td["lastSuccess"].replace("Z", "+00:00"))
                hours_since = (now - last_ok).total_seconds() / 3600
                td["stale"] = hours_since > threshold_h
            except (ValueError, TypeError):
                td["stale"] = True
        else:
            td["stale"] = True  # never succeeded

        if td["stale"]:
            all_healthy = False

    return {
        "healthy": all_healthy,
        "tasks": task_data,
        "checkedAtUtc": _utc_now_iso(),
    }
