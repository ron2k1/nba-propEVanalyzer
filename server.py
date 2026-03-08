#!/usr/bin/env python3
"""
Local API + frontend server for nba_mod.py.

Run:
  python server.py
Then open:
  http://127.0.0.1:8787
"""

import json
import logging
import mimetypes
import os
import subprocess
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.pipeline_progress import read_status, write_status, utc_now_iso
from dotenv import load_dotenv
load_dotenv(override=True)


ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Logging — file + console; DEBUG only when NBA_LOG_LEVEL=DEBUG env is set.
# Pipeline trace logs (nba_engine.*) go to data/logs/pipeline.log.
# ---------------------------------------------------------------------------
_LOG_DIR = ROOT / "data" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_log_level = getattr(logging, os.getenv("NBA_LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(str(_LOG_DIR / "pipeline.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
WEB_DIR = ROOT / "web"
NBA_SCRIPT = ROOT / "nba_mod.py"
DEFAULT_TIMEOUT_SEC = 300
LONG_TIMEOUT_SEC = 1800
DEFAULT_ODDS_MARKETS = "h2h,spreads,totals"
DEFAULT_MAIN_BOOKMAKERS = "betmgm,draftkings,fanduel"
PIPELINE_STATUS_PATH = _LOG_DIR / "pipeline_status.json"

# Server-side lock for long-running commands — prevents duplicate spawns
import threading
_long_running_lock = threading.Lock()
_task_state_lock = threading.Lock()
_current_task = {
    "process": None,
    "taskId": None,
    "taskName": None,
    "cancelRequested": False,
}


def _default_pipeline_status():
    return {
        "success": True,
        "busy": False,
        "taskId": None,
        "taskName": None,
        "currentCommand": None,
        "stage": "idle",
        "message": "No pipeline task running.",
        "updatedAtUtc": utc_now_iso(),
    }


def _read_pipeline_status():
    status = read_status(str(PIPELINE_STATUS_PATH)) or {}
    if not status:
        status = _default_pipeline_status()
    status["busy"] = bool(_long_running_lock.locked())
    return status


def _write_pipeline_status(payload):
    write_status(str(PIPELINE_STATUS_PATH), payload)


def _set_active_task(process, *, task_id, task_name):
    with _task_state_lock:
        _current_task["process"] = process
        _current_task["taskId"] = task_id
        _current_task["taskName"] = task_name
        _current_task["cancelRequested"] = False


def _clear_active_task(task_id=None):
    with _task_state_lock:
        if task_id and _current_task.get("taskId") != task_id:
            return
        _current_task["process"] = None
        _current_task["taskId"] = None
        _current_task["taskName"] = None
        _current_task["cancelRequested"] = False


def _cancel_active_task():
    with _task_state_lock:
        proc = _current_task.get("process")
        task_id = _current_task.get("taskId")
        task_name = _current_task.get("taskName")
        if not proc or proc.poll() is not None:
            return {"success": False, "error": "No pipeline task is currently running."}
        _current_task["cancelRequested"] = True

    current = _read_pipeline_status()
    current.update({
        "taskId": task_id,
        "taskName": task_name,
        "busy": True,
        "stage": "cancelling",
        "message": "Cancellation requested. Waiting for the current task to stop.",
        "cancelRequested": True,
        "updatedAtUtc": utc_now_iso(),
    })
    _write_pipeline_status(current)

    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return {
            "success": True,
            "taskId": task_id,
            "taskName": task_name,
            "message": "Pipeline cancellation requested.",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _lean_rundown(limit=10):
    """Call LLM to summarize today's model leans."""
    try:
        from core.nba_bet_tracking import best_today
        from core.nba_llm_engine import _llm_call
    except Exception as e:
        return {"success": False, "error": f"Import failed: {e}"}

    result = best_today(limit=max(limit, 20))
    leans = result.get("modelLeans") or []
    if not leans:
        return {"success": False, "error": "No model leans available for today."}

    top = leans[:limit]
    lean_lines = []
    for i, l in enumerate(top, 1):
        lean_lines.append(
            f"{i}. {l['playerName']} — {(l.get('stat') or '').upper()} "
            f"{(l.get('recommendedSide') or '').upper()} {l.get('line')} "
            f"(proj {l.get('projection')}, edge {l.get('edge')}, "
            f"bin {l.get('bin')}, blocked: {l.get('policyRejectReason', 'n/a')})"
        )
    lean_text = "\n".join(lean_lines)

    system_prompt = (
        "You are an NBA prop betting analyst. You are reviewing model leans — "
        "signals with positive expected value that were blocked by betting policy "
        "(wrong probability bin, stat not in whitelist, etc). "
        "Give a brief, actionable rundown. Be direct and concise."
    )
    user_prompt = (
        f"Today's top {len(top)} model leans:\n\n{lean_text}\n\n"
        "For each lean, give a 1-sentence take on whether the edge looks real or is likely noise. "
        "Then summarize: which 2-3 leans look most interesting if policy were relaxed, and why. "
        "Keep the total response under 300 words."
    )

    content, provider, err = _llm_call(system_prompt, user_prompt)
    if err:
        return {"success": False, "error": err}

    return {
        "success": True,
        "provider": provider,
        "leanCount": len(top),
        "rundown": content,
    }


def _parse_nba_command_output(stdout, stderr, return_code):
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    payload = None

    if stdout:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if lines:
            try:
                payload = json.loads(lines[-1])
            except json.JSONDecodeError:
                payload = {
                    "success": False,
                    "error": "Failed to parse nba_mod.py JSON output.",
                    "rawOutput": stdout,
                }

    if payload is None:
        payload = {"success": False, "error": "nba_mod.py returned no JSON output."}

    if return_code != 0 and "error" not in payload:
        payload = {
            "success": False,
            "error": stderr or f"nba_mod.py exited with code {return_code}",
            "rawOutput": stdout,
        }

    if stderr:
        payload.setdefault("stderr", stderr)

    return payload


def _run_nba_command(args, timeout_sec=None):
    cmd = [sys.executable, str(NBA_SCRIPT), *args]
    completed = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_sec or DEFAULT_TIMEOUT_SEC,
    )
    return _parse_nba_command_output(completed.stdout, completed.stderr, completed.returncode)


def _run_tracked_nba_command(args, *, task_name, timeout_sec=None):
    task_id = f"{task_name}-{int(time.time() * 1000)}"
    started_at = utc_now_iso()
    cmd_args = list(args)
    if "--progress-file" not in cmd_args:
        cmd_args.extend(["--progress-file", str(PIPELINE_STATUS_PATH)])
    cmd = [sys.executable, str(NBA_SCRIPT), *cmd_args]

    _write_pipeline_status({
        "success": True,
        "busy": True,
        "taskId": task_id,
        "taskName": task_name,
        "currentCommand": task_name,
        "stage": "starting",
        "message": f"Starting {task_name}.",
        "startedAtUtc": started_at,
        "updatedAtUtc": started_at,
    })

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _set_active_task(proc, task_id=task_id, task_name=task_name)

    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec or DEFAULT_TIMEOUT_SEC)
        payload = _parse_nba_command_output(stdout, stderr, proc.returncode)
    except subprocess.TimeoutExpired:
        with _task_state_lock:
            _current_task["cancelRequested"] = True
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        payload = {"success": False, "error": "Request timed out calling nba_mod.py."}
        current = _read_pipeline_status()
        current.update({
            "taskId": task_id,
            "taskName": task_name,
            "currentCommand": task_name,
            "busy": False,
            "stage": "timeout",
            "message": "Pipeline task timed out.",
            "cancelRequested": False,
            "finishedAtUtc": utc_now_iso(),
            "updatedAtUtc": utc_now_iso(),
        })
        _write_pipeline_status(current)
        return payload
    finally:
        _clear_active_task(task_id)

    current = _read_pipeline_status()
    cancel_requested = bool(current.get("cancelRequested"))
    finished_at = utc_now_iso()

    if cancel_requested:
        payload = {
            "success": False,
            "error": "Pipeline task was cancelled.",
        }
        current.update({
            "taskId": task_id,
            "taskName": task_name,
            "busy": False,
            "stage": "cancelled",
            "message": "Pipeline task was cancelled.",
            "cancelRequested": True,
            "finishedAtUtc": finished_at,
            "updatedAtUtc": finished_at,
        })
    else:
        current.update({
            "taskId": task_id,
            "taskName": task_name,
            "busy": False,
            "stage": "completed" if payload.get("success") else "failed",
            "message": "Pipeline task finished." if payload.get("success") else payload.get("error", "Pipeline task failed."),
            "cancelRequested": False,
            "finishedAtUtc": finished_at,
            "updatedAtUtc": finished_at,
            "lastResult": payload,
        })
    _write_pipeline_status(current)
    return payload


_write_pipeline_status(_default_pipeline_status())


class NbaRequestHandler(BaseHTTPRequestHandler):
    server_version = "NbaPipelineServer/1.0"

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if not path.exists() or not path.is_file():
            self._send_json(404, {"success": False, "error": "File not found."})
            return

        content = path.read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        self.send_response(200)
        self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "Invalid Content-Length header."
        if length <= 0:
            return None, "Request body is required."

        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")), None
        except json.JSONDecodeError:
            return None, "Body must be valid JSON."

    def _serve_static(self, path):
        if path == "/":
            return self._send_file(WEB_DIR / "index.html")

        static_path = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR.resolve() not in static_path.parents and static_path != WEB_DIR.resolve():
            self._send_json(403, {"success": False, "error": "Forbidden."})
            return
        self._send_file(static_path)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == "/api/health":
                return self._send_json(
                    200,
                    {
                        "success": True,
                        "service": "nba-pipeline-ui",
                        "nbaScriptPath": str(NBA_SCRIPT),
                        "nbaScriptExists": NBA_SCRIPT.exists(),
                    },
                )

            if path == "/api/games":
                return self._send_json(200, _run_nba_command(["games"]))

            if path == "/api/teams":
                return self._send_json(200, _run_nba_command(["teams"]))

            if path == "/api/players":
                return self._send_json(200, _run_nba_command(["players"]))

            if path == "/api/player_lookup":
                name_query = (query.get("q") or [""])[0].strip()
                if not name_query:
                    return self._send_json(400, {"success": False, "error": "q query param is required."})
                limit = (query.get("limit") or ["20"])[0].strip() or "20"
                return self._send_json(200, _run_nba_command(["player_lookup", name_query, limit]))

            if path == "/api/injury_news":
                team = (query.get("team") or [""])[0].strip().upper()
                if not team:
                    return self._send_json(400, {"success": False, "error": "team query param is required."})
                lookback = (query.get("lookbackHours") or ["24"])[0].strip() or "24"
                return self._send_json(200, _run_nba_command(["injury_news", team, lookback]))

            if path == "/api/usage_adjust_news":
                player = (query.get("player") or [""])[0].strip()
                team = (query.get("team") or [""])[0].strip().upper()
                if not player or not team:
                    return self._send_json(
                        400,
                        {"success": False, "error": "player and team query params are required."},
                    )
                lookback = (query.get("lookbackHours") or ["24"])[0].strip() or "24"
                return self._send_json(200, _run_nba_command(["usage_adjust_news", player, team, lookback]))

            if path == "/api/team_players":
                team_ids = (query.get("teamIds") or [""])[0].strip()
                if not team_ids:
                    return self._send_json(400, {"success": False, "error": "teamIds query param is required."})
                return self._send_json(200, _run_nba_command(["team_players", team_ids]))

            if path == "/api/odds":
                regions = (query.get("regions") or ["us"])[0].strip() or "us"
                markets = (query.get("markets") or [DEFAULT_ODDS_MARKETS])[0].strip() or DEFAULT_ODDS_MARKETS
                bookmakers = (query.get("bookmakers") or [DEFAULT_MAIN_BOOKMAKERS])[0].strip() or DEFAULT_MAIN_BOOKMAKERS
                sport = (query.get("sport") or ["basketball_nba"])[0].strip() or "basketball_nba"
                args = ["odds", regions, markets, bookmakers, sport]
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/odds_live":
                regions = (query.get("regions") or ["us"])[0].strip() or "us"
                markets = (query.get("markets") or [DEFAULT_ODDS_MARKETS])[0].strip() or DEFAULT_ODDS_MARKETS
                bookmakers = (query.get("bookmakers") or [DEFAULT_MAIN_BOOKMAKERS])[0].strip() or DEFAULT_MAIN_BOOKMAKERS
                sport = (query.get("sport") or ["basketball_nba"])[0].strip() or "basketball_nba"
                max_events = (query.get("maxEvents") or ["8"])[0].strip() or "8"
                args = ["odds_live", regions, markets, bookmakers, sport, max_events]
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/best_today":
                limit = (query.get("limit") or ["15"])[0].strip() or "15"
                date_str = (query.get("date") or [""])[0].strip()
                args = ["best_today", limit]
                if date_str:
                    args.append(date_str)
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/results_yesterday":
                limit = (query.get("limit") or ["50"])[0].strip() or "50"
                date_str = (query.get("date") or [""])[0].strip()
                args = ["results_yesterday", limit]
                if date_str:
                    args.append(date_str)
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/starter_accuracy":
                date_str = (query.get("date") or [""])[0].strip()
                bookmakers = (query.get("bookmakers") or [DEFAULT_MAIN_BOOKMAKERS])[0].strip() or DEFAULT_MAIN_BOOKMAKERS
                regions = (query.get("regions") or ["us"])[0].strip() or "us"
                sport = (query.get("sport") or ["basketball_nba"])[0].strip() or "basketball_nba"
                model_variant = (query.get("modelVariant") or ["full"])[0].strip() or "full"
                args = ["starter_accuracy"]
                if date_str:
                    args.append(date_str)
                args.extend([bookmakers, regions, sport, model_variant])
                return self._send_json(200, _run_nba_command(args, timeout_sec=LONG_TIMEOUT_SEC))

            if path == "/api/settle_yesterday":
                date_str = (query.get("date") or [""])[0].strip()
                args = ["settle_yesterday"]
                if date_str:
                    args.append(date_str)
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/paper_summary":
                window_days = (query.get("windowDays") or ["14"])[0].strip() or "14"
                args = ["paper_summary", "--window-days", window_days]
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/journal_gate":
                window_days = (query.get("windowDays") or ["14"])[0].strip() or "14"
                args = ["journal_gate", "--window-days", window_days]
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/leans_for_date":
                date_str = (query.get("date") or [""])[0].strip()
                limit = int((query.get("limit") or ["50"])[0].strip() or "50")
                try:
                    from core.nba_bet_tracking import leans_for_date
                    leans = leans_for_date(date_str or None, limit=limit)
                    wins = sum(1 for l in leans if l.get("result") == "win")
                    losses = sum(1 for l in leans if l.get("result") == "loss")
                    pushes = sum(1 for l in leans if l.get("result") == "push")
                    settled = wins + losses + pushes
                    pnl = sum(l.get("pnl") or 0 for l in leans)
                    return self._send_json(200, {
                        "success": True,
                        "date": date_str or "today",
                        "leans": leans,
                        "count": len(leans),
                        "settled": settled,
                        "wins": wins, "losses": losses, "pushes": pushes,
                        "pnl": round(pnl, 2),
                        "hitRate": round(100.0 * wins / (wins + losses), 2) if (wins + losses) > 0 else None,
                    })
                except Exception as e:
                    return self._send_json(200, {"success": False, "error": str(e)})

            if path == "/api/lean_rundown":
                limit = int((query.get("limit") or ["10"])[0].strip() or "10")
                result = _lean_rundown(limit)
                return self._send_json(200, result)

            if path == "/api/pipeline_status":
                return self._send_json(200, _read_pipeline_status())

            if path == "/api/pipeline_cancel":
                return self._send_json(200, _cancel_active_task())

            if path == "/api/roster_sweep":
                if not _long_running_lock.acquire(blocking=False):
                    return self._send_json(409, {"success": False, "error": "Roster sweep already running."})
                try:
                    date_str = (query.get("date") or [""])[0].strip()
                    args = ["roster_sweep"]
                    if date_str:
                        args.append(date_str)
                    return self._send_json(
                        200,
                        _run_tracked_nba_command(args, task_name="roster_sweep", timeout_sec=LONG_TIMEOUT_SEC),
                    )
                finally:
                    _long_running_lock.release()

            if path == "/api/collect_lines":
                if not _long_running_lock.acquire(blocking=False):
                    return self._send_json(409, {"success": False, "error": "Another pipeline task is running."})
                try:
                    books = (query.get("books") or [DEFAULT_MAIN_BOOKMAKERS])[0].strip() or DEFAULT_MAIN_BOOKMAKERS
                    stats = (query.get("stats") or ["pts,reb,ast,pra"])[0].strip() or "pts,reb,ast,pra"
                    args = ["collect_lines", "--books", books, "--stats", stats]
                    return self._send_json(200, _run_nba_command(args))
                finally:
                    _long_running_lock.release()

            if path == "/api/daily_ops":
                if not _long_running_lock.acquire(blocking=False):
                    return self._send_json(409, {"success": False, "error": "Another pipeline task is running."})
                try:
                    dry_run = (query.get("dryRun") or ["false"])[0].strip().lower()
                    args = ["daily_ops"]
                    if dry_run == "true":
                        args.append("--dry-run")
                    return self._send_json(
                        200,
                        _run_tracked_nba_command(args, task_name="daily_ops", timeout_sec=LONG_TIMEOUT_SEC),
                    )
                finally:
                    _long_running_lock.release()

            if path == "/api/top_picks":
                limit = (query.get("limit") or ["5"])[0].strip() or "5"
                args = ["top_picks", limit]
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/lean_clv_report":
                window_days = int((query.get("windowDays") or ["14"])[0].strip() or "14")
                source = (query.get("source") or ["live"])[0].strip() or "live"
                try:
                    if source == "backtest":
                        from core.nba_bet_tracking import backtest_lean_clv_report
                        result = backtest_lean_clv_report(source="backtest")
                    else:
                        from core.nba_decision_journal import DecisionJournal
                        dj = DecisionJournal()
                        try:
                            result = dj.lean_accuracy_clv(window_days=window_days)
                        finally:
                            dj.close()
                    return self._send_json(200, {"success": True, **result})
                except Exception as e:
                    return self._send_json(200, {"success": False, "error": str(e)})

            if path == "/api/enrich_journal_clv":
                try:
                    from core.nba_bet_tracking import enrich_journal_clv
                    result = enrich_journal_clv()
                    return self._send_json(200, result)
                except Exception as e:
                    return self._send_json(200, {"success": False, "error": str(e)})

            if path == "/api/line_movement":
                player = (query.get("player") or [""])[0].strip()
                stat_q = (query.get("stat") or [""])[0].strip().lower()
                date_str = (query.get("date") or [""])[0].strip()
                if not player or not stat_q:
                    return self._send_json(400, {"success": False, "error": "player and stat query params required."})
                try:
                    from core.nba_line_store import LineStore
                    ls = LineStore()
                    if not date_str:
                        from datetime import datetime as _dt, timezone as _tz
                        date_str = _dt.now(_tz.utc).strftime("%Y-%m-%d")
                    snaps = ls.get_snapshots(date_str, stat=stat_q, player_name=player)
                    # Group by book for multi-series chart
                    by_book = {}
                    for s in snaps:
                        bk = s.get("book") or s.get("bookmaker") or "unknown"
                        if bk not in by_book:
                            by_book[bk] = []
                        by_book[bk].append({
                            "timestamp": s.get("timestamp_utc"),
                            "line": s.get("line"),
                            "overOdds": s.get("over_odds"),
                            "underOdds": s.get("under_odds"),
                        })
                    return self._send_json(200, {
                        "success": True,
                        "player": player,
                        "stat": stat_q,
                        "date": date_str,
                        "books": by_book,
                        "totalSnapshots": len(snaps),
                    })
                except Exception as e:
                    return self._send_json(200, {"success": False, "error": str(e)})

            if path == "/api/backfill_lean_clv":
                try:
                    from core.nba_decision_journal import DecisionJournal
                    from core.nba_odds_store import OddsStore
                    odds_store = OddsStore()
                    dj = DecisionJournal()
                    try:
                        result = dj.backfill_lean_clv(odds_store)
                    finally:
                        dj.close()
                        odds_store.close()
                    return self._send_json(200, result)
                except Exception as e:
                    return self._send_json(200, {"success": False, "error": str(e)})

            return self._serve_static(path)
        except subprocess.TimeoutExpired:
            self._send_json(504, {"success": False, "error": "Request timed out calling nba_mod.py."})
        except Exception as exc:
            self._send_json(
                500,
                {"success": False, "error": str(exc), "traceback": traceback.format_exc()},
            )

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            body, error = self._read_json_body()
            if error:
                return self._send_json(400, {"success": False, "error": error})

            if path == "/api/prop_ev":
                required = ["opponentAbbr", "isHome", "stat", "line", "overOdds", "underOdds"]
                missing = [k for k in required if k not in body]
                if missing:
                    return self._send_json(400, {"success": False, "error": f"Missing fields: {', '.join(missing)}"})

                raw_player_id = body.get("playerId")
                raw_player_name = str(body.get("playerName", "")).strip()
                player_arg = None
                if raw_player_id is not None and str(raw_player_id).strip() != "":
                    try:
                        player_arg = str(int(raw_player_id))
                    except (TypeError, ValueError):
                        return self._send_json(400, {"success": False, "error": "playerId must be numeric if provided."})
                elif raw_player_name:
                    player_arg = raw_player_name
                else:
                    return self._send_json(400, {"success": False, "error": "Provide playerId or playerName."})

                args = [
                    "prop_ev",
                    player_arg,
                    str(body["opponentAbbr"]).upper(),
                    "1" if bool(body["isHome"]) else "0",
                    str(body["stat"]).lower(),
                    str(float(body["line"])),
                    str(int(body["overOdds"])),
                    str(int(body["underOdds"])),
                    "1" if bool(body.get("isB2b", False)) else "0",
                ]
                player_team_abbr = str(body.get("playerTeamAbbr", "")).upper().strip()
                reference_book = str(body.get("referenceBook", "")).strip()
                if player_team_abbr:
                    args.append(player_team_abbr)
                    if reference_book:
                        args.append(reference_book)
                mins_mult = body.get("minutesMultiplier")
                if mins_mult is not None:
                    try:
                        args.extend(["--mins-mult", str(float(mins_mult))])
                    except (TypeError, ValueError):
                        pass
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/prop_ev_ml":
                required = ["opponentAbbr", "isHome", "stat", "line", "overOdds", "underOdds"]
                missing = [k for k in required if k not in body]
                if missing:
                    return self._send_json(400, {"success": False, "error": f"Missing fields: {', '.join(missing)}"})

                raw_player_id = body.get("playerId")
                raw_player_name = str(body.get("playerName", "")).strip()
                player_arg = None
                if raw_player_id is not None and str(raw_player_id).strip() != "":
                    try:
                        player_arg = str(int(raw_player_id))
                    except (TypeError, ValueError):
                        return self._send_json(400, {"success": False, "error": "playerId must be numeric if provided."})
                elif raw_player_name:
                    player_arg = raw_player_name
                else:
                    return self._send_json(400, {"success": False, "error": "Provide playerId or playerName."})

                args = [
                    "prop_ev_ml",
                    player_arg,
                    str(body["opponentAbbr"]).upper(),
                    "1" if bool(body["isHome"]) else "0",
                    str(body["stat"]).lower(),
                    str(float(body["line"])),
                    str(int(body["overOdds"])),
                    str(int(body["underOdds"])),
                    "1" if bool(body.get("isB2b", False)) else "0",
                ]
                model_path = str(body.get("modelPath", "")).strip()
                if model_path:
                    args.append(model_path)
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/auto_sweep":
                required = ["opponentAbbr", "isHome", "stat", "playerTeamAbbr"]
                missing = [k for k in required if k not in body]
                if missing:
                    return self._send_json(400, {"success": False, "error": f"Missing fields: {', '.join(missing)}"})

                raw_player_id = body.get("playerId")
                raw_player_name = str(body.get("playerName", "")).strip()
                player_arg = None
                if raw_player_id is not None and str(raw_player_id).strip() != "":
                    try:
                        player_arg = str(int(raw_player_id))
                    except (TypeError, ValueError):
                        return self._send_json(400, {"success": False, "error": "playerId must be numeric if provided."})
                elif raw_player_name:
                    player_arg = raw_player_name
                else:
                    return self._send_json(400, {"success": False, "error": "Provide playerId or playerName."})

                regions = str(body.get("regions", "us")).strip() or "us"
                bookmakers = str(body.get("bookmakers", "")).strip() or DEFAULT_MAIN_BOOKMAKERS
                sport = str(body.get("sport", "basketball_nba")).strip() or "basketball_nba"
                top_n = body.get("topN", 15)
                try:
                    top_n = int(top_n)
                except (TypeError, ValueError):
                    top_n = 15

                args = [
                    "auto_sweep",
                    player_arg,
                    str(body.get("playerTeamAbbr", "")).upper(),
                    str(body.get("opponentAbbr", "")).upper(),
                    "1" if bool(body["isHome"]) else "0",
                    str(body.get("stat", "")).lower(),
                    "1" if bool(body.get("isB2b", False)) else "0",
                    regions,
                    bookmakers,
                    sport,
                    str(top_n),
                ]
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/parlay_ev":
                legs = body.get("legs")
                if not isinstance(legs, list):
                    return self._send_json(400, {"success": False, "error": "legs must be a JSON array."})
                args = ["parlay_ev", json.dumps(legs, separators=(",", ":"))]
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/live_projection":
                required = ["playerTeamAbbr", "opponentAbbr", "isHome", "stat"]
                missing = [k for k in required if k not in body]
                if missing:
                    return self._send_json(400, {"success": False, "error": f"Missing fields: {', '.join(missing)}"})

                raw_player_id = body.get("playerId")
                raw_player_name = str(body.get("playerName", "")).strip()
                player_arg = None
                if raw_player_id is not None and str(raw_player_id).strip() != "":
                    try:
                        player_arg = str(int(raw_player_id))
                    except (TypeError, ValueError):
                        return self._send_json(400, {"success": False, "error": "playerId must be numeric if provided."})
                elif raw_player_name:
                    player_arg = raw_player_name
                else:
                    return self._send_json(400, {"success": False, "error": "Provide playerId or playerName."})

                args = [
                    "live_projection",
                    player_arg,
                    str(body["playerTeamAbbr"]).upper(),
                    str(body["opponentAbbr"]).upper(),
                    "1" if bool(body["isHome"]) else "0",
                    str(body["stat"]).lower(),
                ]
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/llm_analyze":
                required = ["teamAbbr", "opponentAbbr", "isHome", "stat", "line"]
                missing = [k for k in required if k not in body]
                if missing:
                    return self._send_json(400, {"success": False, "error": f"Missing fields: {', '.join(missing)}"})

                raw_player_id = body.get("playerId")
                raw_player_name = str(body.get("playerName", "")).strip()
                player_arg = None
                if raw_player_id is not None and str(raw_player_id).strip() != "":
                    try:
                        player_arg = str(int(raw_player_id))
                    except (TypeError, ValueError):
                        return self._send_json(400, {"success": False, "error": "playerId must be numeric if provided."})
                elif raw_player_name:
                    player_arg = raw_player_name
                else:
                    return self._send_json(400, {"success": False, "error": "Provide playerId or playerName."})

                args = [
                    "llm_analyze",
                    player_arg,
                    str(body["teamAbbr"]).upper(),
                    str(body["opponentAbbr"]).upper(),
                    "1" if bool(body["isHome"]) else "0",
                    str(body["stat"]).lower(),
                    str(float(body["line"])),
                    str(int(body.get("overOdds", -110))),
                    str(int(body.get("underOdds", -110))),
                ]
                return self._send_json(200, _run_nba_command(args, timeout_sec=120))

            self._send_json(404, {"success": False, "error": "Unknown endpoint."})
        except subprocess.TimeoutExpired:
            self._send_json(504, {"success": False, "error": "Request timed out calling nba_mod.py."})
        except Exception as exc:
            self._send_json(
                500,
                {"success": False, "error": str(exc), "traceback": traceback.format_exc()},
            )


def run(host="127.0.0.1", port=8787):
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), NbaRequestHandler)
    print(f"Server listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) == 3:
        run(sys.argv[1], int(sys.argv[2]))
    else:
        run()
