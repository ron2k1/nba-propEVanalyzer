#!/usr/bin/env python3
"""Quality gate for catching likely hallucination artifacts and regressions."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


FATAL_PATTERNS = [
    (
        "llm_projection_stub",
        re.compile(r"projection\s*=\s*line\s*,\s*#\s*no model projection", re.IGNORECASE),
    ),
    (
        "llm_ev_stub",
        re.compile(
            r'ev_data\s*=\s*\{"over":\s*\{"evPercent":\s*None\},\s*"under":\s*\{"evPercent":\s*None\}\}'
        ),
    ),
    (
        "llm_line_arg_bug",
        re.compile(
            r'if len\(argv\)\s*<\s*7:\s*\n\s*return \{"success": False, "error": "Usage: llm_line',
            re.MULTILINE,
        ),
    ),
]

def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _python_exe() -> str:
    win_venv = ROOT / ".venv" / "Scripts" / "python.exe"
    if win_venv.exists():
        return str(win_venv)
    return sys.executable


def _tracked_py_files() -> list[str]:
    cp = _run(["git", "ls-files", "*.py"], timeout=30)
    if cp.returncode != 0:
        return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py") if ".venv" not in str(p)]
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def _scan_patterns() -> list[dict]:
    findings: list[dict] = []
    for path in _tracked_py_files():
        file_path = ROOT / path
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for name, pattern in FATAL_PATTERNS:
            if pattern.search(text):
                findings.append({"file": path, "rule": name})
    return findings


def _compile_python(pyexe: str) -> tuple[bool, str]:
    files = _tracked_py_files()
    if not files:
        return True, "no python files"
    # Avoid command-line length issues by chunking.
    chunk_size = 80
    for i in range(0, len(files), chunk_size):
        chunk = files[i : i + chunk_size]
        cp = _run([pyexe, "-m", "py_compile", *chunk], timeout=180)
        if cp.returncode != 0:
            return False, (cp.stderr or cp.stdout).strip()
    return True, "ok"


def _check_js_syntax() -> tuple[bool, str]:
    node = _run(["node", "--version"], timeout=10)
    if node.returncode != 0:
        return True, "node missing (skipped)"
    js_files = ["web/app.js"] + sorted(
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in (ROOT / "web" / "modules").glob("*.js")
    )
    failures: list[str] = []
    for js_file in js_files:
        cp = _run(["node", "--check", js_file], timeout=30)
        if cp.returncode != 0:
            failures.append(f"{js_file}: {(cp.stderr or cp.stdout).strip()}")
    if failures:
        return False, "; ".join(failures)
    return True, f"ok ({len(js_files)} files)"


def _check_js_imports() -> tuple[bool, str]:
    """Verify that all ES module imports in web/app.js resolve to existing files."""
    app_js = ROOT / "web" / "app.js"
    if not app_js.exists():
        return False, "web/app.js not found"
    text = app_js.read_text(encoding="utf-8", errors="replace")
    import_re = re.compile(r"""import\s+\w+\s+from\s+['"](\./[^'"]+)['"]""")
    missing: list[str] = []
    checked = 0
    for m in import_re.finditer(text):
        rel_path = m.group(1)
        abs_path = (app_js.parent / rel_path).resolve()
        checked += 1
        if not abs_path.exists():
            missing.append(rel_path)
    if not checked:
        return True, "no imports found (skipped)"
    if missing:
        return False, f"missing modules: {', '.join(missing)}"
    return True, f"ok ({checked} imports verified)"


def _json_from_last_line(raw: str) -> dict | None:
    lines = [x.strip() for x in (raw or "").splitlines() if x.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except Exception:
        return None


def _parse_json_with_fallback(stdout: str) -> tuple[dict, str | None]:
    """
    Parse a JSON object from subprocess stdout.
    First attempts to parse the full stdout; if that fails, scans backwards
    for the last '{' to skip any banner text that precedes the JSON payload.
    Returns (parsed_dict, parse_error_or_None).
    """
    text = (stdout or "").strip()
    if not text:
        return {}, None
    try:
        return json.loads(text), None
    except Exception as first_err:
        brace_idx = text.rfind("{")
        if brace_idx >= 0:
            try:
                return json.loads(text[brace_idx:]), None
            except Exception as second_err:
                return {}, f"{first_err}; fallback={second_err}"
        return {}, str(first_err)


def _smoke_llm(pyexe: str) -> tuple[bool, str]:
    llm_line = _run([pyexe, "nba_mod.py", "llm_line", "203999", "pts", "30", "28.5"], timeout=240)
    obj1 = _json_from_last_line(llm_line.stdout)
    if llm_line.returncode != 0 or not obj1 or obj1.get("success") is not True:
        detail = llm_line.stderr.strip() or llm_line.stdout.strip() or "llm_line failed"
        return False, detail

    llm_analyze = _run(
        [pyexe, "nba_mod.py", "llm_analyze", "203999", "DEN", "LAL", "1", "pts", "30", "-110", "-110"],
        timeout=300,
    )
    obj2 = _json_from_last_line(llm_analyze.stdout)
    if llm_analyze.returncode != 0 or not obj2 or obj2.get("success") is not True:
        detail = llm_analyze.stderr.strip() or llm_analyze.stdout.strip() or "llm_analyze failed"
        return False, detail

    if obj2.get("projectionSource") not in {"model_projection", "line_fallback"}:
        return False, f"unexpected projectionSource: {obj2.get('projectionSource')}"

    return True, f"providers: line={obj1.get('provider')}, analyze_line={((obj2.get('lineReasoning') or {}).get('provider'))}"


def _smoke_gamelog_fallback(pyexe: str) -> tuple[bool, str]:
    script = (
        "import json, os\n"
        "from core import nba_data_collection as dc\n"
        "player_id = 1628973\n"
        "season = '2025-26'\n"
        "cache_key = f'gamelog_{player_id}_{season}_25_full'\n"
        "cache_path = dc._cache_path(cache_key)\n"
        "try:\n"
        "    if os.path.exists(cache_path):\n"
        "        os.remove(cache_path)\n"
        "except Exception:\n"
        "    pass\n"
        "orig_retry = dc.retry_api_call\n"
        "dc.retry_api_call = lambda *a, **k: (_ for _ in ()).throw(ConnectionError('quality_gate_simulated_outage'))\n"
        "try:\n"
        "    out = dc.get_player_game_log(player_id, season=season, last_n=25)\n"
        "finally:\n"
        "    dc.retry_api_call = orig_retry\n"
        "payload = {\n"
        "    'success': bool(out.get('success')),\n"
        "    'source': out.get('source'),\n"
        "    'gamesPlayed': int(out.get('gamesPlayed') or len(out.get('gameLogs') or [])),\n"
        "    'error': out.get('error'),\n"
        "}\n"
        "print(json.dumps(payload))\n"
    )
    cp = _run([pyexe, "-c", script], timeout=120)
    obj = _json_from_last_line(cp.stdout)
    if cp.returncode != 0 or not obj:
        detail = cp.stderr.strip() or cp.stdout.strip() or "gamelog fallback smoke failed"
        return False, detail
    ok = (
        obj.get("success") is True
        and obj.get("source") == "local_index_fallback"
        and int(obj.get("gamesPlayed") or 0) > 0
    )
    if not ok:
        return False, json.dumps(obj, separators=(",", ":"))
    return True, f"source={obj.get('source')} games={obj.get('gamesPlayed')}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Run repository quality gate checks.")
    ap.add_argument("--full", action="store_true", help="Include slower LLM smoke tests.")
    ap.add_argument("--json", action="store_true", help="Print JSON report.")
    args = ap.parse_args()

    pyexe = _python_exe()
    report = {"ok": True, "checks": []}

    ok, msg = _compile_python(pyexe)
    report["checks"].append({"name": "python_compile", "ok": ok, "detail": msg})
    report["ok"] = report["ok"] and ok

    ok, msg = _check_js_syntax()
    report["checks"].append({"name": "js_syntax", "ok": ok, "detail": msg})
    report["ok"] = report["ok"] and ok

    ok, msg = _check_js_imports()
    report["checks"].append({"name": "js_imports", "ok": ok, "detail": msg})
    report["ok"] = report["ok"] and ok

    findings = _scan_patterns()
    patt_ok = len(findings) == 0
    report["checks"].append({"name": "hallucination_patterns", "ok": patt_ok, "detail": findings})
    report["ok"] = report["ok"] and patt_ok

    if os.environ.get("CI"):
        report["checks"].append({"name": "gamelog_fallback_smoke", "ok": True, "detail": "skipped in CI (no local index)"})
    else:
        ok, msg = _smoke_gamelog_fallback(pyexe)
        report["checks"].append({"name": "gamelog_fallback_smoke", "ok": ok, "detail": msg})
        report["ok"] = report["ok"] and ok

    if args.full:
        ok, msg = _smoke_llm(pyexe)
        report["checks"].append({"name": "llm_smoke", "ok": ok, "detail": msg})
        report["ok"] = report["ok"] and ok

        # LineStore → OddsStore bridge smoke
        cp = _run([pyexe, "scripts/validate_line_bridge.py"], timeout=120)
        vj, parse_error = _parse_json_with_fallback(cp.stdout)
        bridge_ok = cp.returncode == 0 and isinstance(vj, dict) and vj.get("ok") is True
        if parse_error:
            bridge_detail = {"parseError": parse_error, "stdoutTail": (cp.stdout or "")[-300:]}
        else:
            bridge_detail = vj.get("checks", []) if isinstance(vj, dict) else []
        report["checks"].append({"name": "line_bridge_smoke", "ok": bridge_ok, "detail": bridge_detail})
        report["ok"] = report["ok"] and bridge_ok

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"QUALITY_GATE_OK={report['ok']}")
        for chk in report["checks"]:
            print(f"- {chk['name']}: {'PASS' if chk['ok'] else 'FAIL'}")
            if chk["detail"] not in ("ok", "node missing (skipped)"):
                print(f"  {chk['detail']}")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

