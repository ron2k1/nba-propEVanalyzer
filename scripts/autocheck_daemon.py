#!/usr/bin/env python3
"""Background watcher that runs quality_gate.py on source changes.

Polls watched paths for mtime/size changes, debounces rapid edits,
and runs the quality gate with cooldown between runs.  Uses a PID
lock file to guarantee only one instance at a time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs" / "autocheck"
LATEST_JSON = LOG_DIR / "latest.json"
HISTORY_NDJSON = LOG_DIR / "history.ndjson"
LOCK_FILE = LOG_DIR / "daemon.lock"

WATCH_PATHS = [
    "core",
    "nba_cli",
    "scripts",
    "web",
    "server.py",
    "nba_mod.py",
    "run_ui.ps1",
    "README.md",
    "requirements.txt",
]
WATCH_SUFFIXES = {".py", ".js", ".html", ".ps1", ".json", ".md", ".txt", ".yml", ".yaml"}
EXCLUDED_DIR_NAMES = {
    ".git",
    ".venv",
    ".nba_cache",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    "data",
    "models",
}

_shutdown_requested = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# PID liveness — Windows-safe via kernel32.OpenProcess
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Return True if *pid* refers to a running process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        ERROR_ACCESS_DENIED = 5
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------------
# Lock file — atomic create, stale-pid recovery
# ---------------------------------------------------------------------------

def _acquire_lock() -> tuple[bool, str]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()

    def _write_lock() -> bool:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": current_pid, "startedAt": _utc_now_iso()}, fh, indent=2)
        return True

    if _write_lock():
        return True, "lock acquired"

    try:
        existing = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        existing_pid = int(existing.get("pid") or 0)
    except Exception:
        existing_pid = 0

    if _pid_alive(existing_pid):
        return False, f"already running (pid={existing_pid})"

    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    if _write_lock():
        return True, f"lock acquired (recovered stale lock from pid={existing_pid})"
    return False, "already running (lock race)"


def _release_lock() -> None:
    try:
        existing = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        if int(existing.get("pid") or 0) == os.getpid():
            LOCK_FILE.unlink(missing_ok=True)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


# ---------------------------------------------------------------------------
# Python executable resolution
# ---------------------------------------------------------------------------

def _resolve_python(python_override: str | None) -> str:
    if python_override:
        return str(Path(python_override).resolve())
    candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    if candidate.exists():
        return str(candidate)
    candidate_posix = ROOT / ".venv" / "bin" / "python"
    if candidate_posix.exists():
        return str(candidate_posix)
    return sys.executable


# ---------------------------------------------------------------------------
# File watching
# ---------------------------------------------------------------------------

def _iter_watch_files() -> list[Path]:
    files: list[Path] = []
    for rel in WATCH_PATHS:
        target = ROOT / rel
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix.lower() in WATCH_SUFFIXES:
                files.append(target)
            continue
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES and not d.startswith(".")]
            for name in filenames:
                if Path(name).suffix.lower() in WATCH_SUFFIXES:
                    files.append(Path(dirpath) / name)
    return files


def _snapshot() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for path in _iter_watch_files():
        try:
            st = path.stat()
            rel = path.relative_to(ROOT).as_posix()
            out[rel] = (int(st.st_mtime_ns), int(st.st_size))
        except (FileNotFoundError, OSError):
            continue
    return out


def _snapshot_digest(snap: dict[str, tuple[int, int]]) -> str:
    h = hashlib.sha256()
    for rel in sorted(snap):
        mtime_ns, size = snap[rel]
        h.update(f"{rel}|{mtime_ns}|{size}\n".encode("utf-8", errors="ignore"))
    return h.hexdigest()


def _diff_files(old: dict[str, tuple[int, int]], new: dict[str, tuple[int, int]]) -> list[str]:
    """Return list of relative paths that changed between snapshots."""
    changed: list[str] = []
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            changed.append(k)
    return changed


# ---------------------------------------------------------------------------
# Quality gate runner
# ---------------------------------------------------------------------------

def _parse_last_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        try:
            return json.loads(lines[-1])
        except Exception:
            pass
    brace_idx = text.rfind("{")
    if brace_idx >= 0:
        try:
            return json.loads(text[brace_idx:])
        except Exception:
            return None
    return None


def _run_quality_gate(pyexe: str, reason: str) -> dict:
    started = time.perf_counter()
    cmd = [pyexe, "scripts/quality_gate.py", "--json"]
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "ts": _utc_now_iso(),
            "reason": reason,
            "ok": False,
            "returnCode": -1,
            "elapsedMs": elapsed_ms,
            "summary": None,
            "stderrTail": "quality_gate.py timed out after 900s",
        }
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    parsed = _parse_last_json(cp.stdout)
    ok = bool(parsed and parsed.get("ok") is True and cp.returncode == 0)
    return {
        "ts": _utc_now_iso(),
        "reason": reason,
        "ok": ok,
        "returnCode": cp.returncode,
        "elapsedMs": elapsed_ms,
        "summary": parsed,
        "stderrTail": (cp.stderr or "").strip()[-1000:],
    }


def _write_result(record: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_JSON.write_text(json.dumps(record, indent=2), encoding="utf-8")
    with HISTORY_NDJSON.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def _run_check(pyexe: str, reason: str) -> dict:
    result = _run_quality_gate(pyexe, reason=reason)
    _write_result(result)
    return result


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def _handle_signal(signum: int, _frame: object) -> None:
    global _shutdown_requested  # noqa: PLW0603
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _print(msg: str) -> None:
    """Print with immediate flush so output appears in non-TTY terminals."""
    print(msg, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch repo and auto-run quality gate.")
    parser.add_argument("--interval", type=float, default=20.0,
                        help="Polling interval in seconds (default: 20).")
    parser.add_argument("--debounce", type=float, default=8.0,
                        help="Wait this long after last change before running (default: 8).")
    parser.add_argument("--cooldown", type=float, default=25.0,
                        help="Minimum seconds between gate runs (default: 25).")
    parser.add_argument("--python", type=str, default=None,
                        help="Override python executable path.")
    parser.add_argument("--once", action="store_true",
                        help="Run one check immediately and exit.")
    parser.add_argument("--skip-start-check", action="store_true",
                        help="Do not run a startup check.")
    args = parser.parse_args()

    pyexe = _resolve_python(args.python)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, _handle_signal)  # type: ignore[attr-defined]

    lock_ok, lock_msg = _acquire_lock()
    if not lock_ok:
        _print(f"AUTOCHECK: {lock_msg}")
        return 0

    _print(f"AUTOCHECK: pid={os.getpid()}  python={pyexe}")
    _print(f"AUTOCHECK: interval={args.interval}s  debounce={args.debounce}s  cooldown={args.cooldown}s")

    try:
        if args.once:
            result = _run_check(pyexe, reason="manual_once")
            _print(json.dumps(result))
            return 0 if result.get("ok") else 1

        if not args.skip_start_check:
            _print("AUTOCHECK: running startup check ...")
            startup_result = _run_check(pyexe, reason="startup")
            status = "PASS" if startup_result.get("ok") else "FAIL"
            _print(f"AUTOCHECK: startup {status}  ({startup_result.get('elapsedMs'):.0f}ms)")

        snapshot_prev = _snapshot()
        digest_prev = _snapshot_digest(snapshot_prev)
        pending_since: float | None = None
        last_run_at = time.monotonic()
        check_count = 0

        _print(f"AUTOCHECK: watching {len(snapshot_prev)} files — daemon running.")
        while not _shutdown_requested:
            time.sleep(max(1.0, args.interval))
            if _shutdown_requested:
                break

            snapshot_now = _snapshot()
            digest_now = _snapshot_digest(snapshot_now)
            if digest_now != digest_prev:
                changed = _diff_files(snapshot_prev, snapshot_now)
                snapshot_prev = snapshot_now
                digest_prev = digest_now
                pending_since = time.monotonic()
                _print(f"AUTOCHECK: {len(changed)} file(s) changed — {', '.join(changed[:5])}"
                       + (f" (+{len(changed)-5} more)" if len(changed) > 5 else ""))

            now = time.monotonic()
            if pending_since is None:
                continue
            if (now - pending_since) < max(0.0, args.debounce):
                continue
            if (now - last_run_at) < max(0.0, args.cooldown):
                continue

            check_count += 1
            _print(f"AUTOCHECK: running check #{check_count} ...")
            result = _run_check(pyexe, reason="filesystem_change")
            status = "PASS" if result.get("ok") else "FAIL"
            _print(f"AUTOCHECK: check #{check_count} {status}  ({result.get('elapsedMs'):.0f}ms)")
            pending_since = None
            last_run_at = time.monotonic()

        _print("AUTOCHECK: shutting down gracefully.")
    finally:
        _release_lock()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
