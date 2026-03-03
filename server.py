#!/usr/bin/env python3
"""
Local API + frontend server for nba_mod.py.

Run:
  python server.py
Then open:
  http://127.0.0.1:8787
"""

import json
import mimetypes
import subprocess
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
load_dotenv(override=True)


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
NBA_SCRIPT = ROOT / "nba_mod.py"
DEFAULT_TIMEOUT_SEC = 300
LONG_TIMEOUT_SEC = 1800
DEFAULT_ODDS_MARKETS = "h2h,spreads,totals"
DEFAULT_MAIN_BOOKMAKERS = "betmgm,draftkings,fanduel"

# Server-side lock for long-running commands — prevents duplicate spawns
import threading
_long_running_lock = threading.Lock()


def _run_nba_command(args, timeout_sec=None):
    cmd = [sys.executable, str(NBA_SCRIPT), *args]
    completed = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_sec or DEFAULT_TIMEOUT_SEC,
    )

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
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

    if completed.returncode != 0 and "error" not in payload:
        payload = {
            "success": False,
            "error": stderr or f"nba_mod.py exited with code {completed.returncode}",
            "rawOutput": stdout,
        }

    if stderr:
        payload.setdefault("stderr", stderr)

    return payload


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

            if path == "/api/roster_sweep":
                if not _long_running_lock.acquire(blocking=False):
                    return self._send_json(409, {"success": False, "error": "Roster sweep already running."})
                try:
                    date_str = (query.get("date") or [""])[0].strip()
                    args = ["roster_sweep"]
                    if date_str:
                        args.append(date_str)
                    return self._send_json(200, _run_nba_command(args, timeout_sec=LONG_TIMEOUT_SEC))
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
                    return self._send_json(200, _run_nba_command(args, timeout_sec=LONG_TIMEOUT_SEC))
                finally:
                    _long_running_lock.release()

            if path == "/api/top_picks":
                limit = (query.get("limit") or ["5"])[0].strip() or "5"
                args = ["top_picks", limit]
                return self._send_json(200, _run_nba_command(args))

            if path == "/api/lightrag_health":
                return self._send_json(200, _run_nba_command(["lightrag_health"]))

            if path == "/api/lightrag_ingest":
                if not _long_running_lock.acquire(blocking=False):
                    return self._send_json(409, {"success": False, "error": "Ingest already running."})
                try:
                    source = (query.get("source") or ["all"])[0].strip() or "all"
                    force = (query.get("force") or ["false"])[0].strip().lower() == "true"
                    args = ["lightrag_ingest", "--source", source]
                    if force:
                        args.append("--force")
                    return self._send_json(200, _run_nba_command(args, timeout_sec=120))
                finally:
                    _long_running_lock.release()

            if path == "/api/lightrag_query":
                q = (query.get("q") or [""])[0].strip()
                if not q:
                    return self._send_json(400, {"success": False, "error": "q query param is required."})
                return self._send_json(200, _run_nba_command(["lightrag_query", q]))

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
