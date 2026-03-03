#!/usr/bin/env python3
"""LightRAG commands: lightrag_ingest, lightrag_query, lightrag_health."""

import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")

_VALID_SOURCES = {"docs", "lessons", "backtests", "journal", "claude_md", "all"}


def _get_base_url():
    return os.environ.get("LIGHTRAG_BASE_URL", "http://localhost:9621")


def _handle_lightrag_ingest(argv):
    """
    lightrag_ingest --source docs|lessons|backtests|journal|claude_md|all [--force]

    Shells out to scripts/lightrag_ingest.py to ingest documents into LightRAG.
    """
    source = None
    force = False

    i = 2
    while i < len(argv):
        if argv[i] == "--source" and i + 1 < len(argv):
            source = argv[i + 1]
            i += 2
        elif argv[i] == "--force":
            force = True
            i += 1
        else:
            i += 1

    if not source:
        return {
            "success": False,
            "error": "Missing --source. Usage: lightrag_ingest --source docs|lessons|backtests|journal|claude_md|all [--force]",
        }

    if source not in _VALID_SOURCES:
        return {
            "success": False,
            "error": f"Invalid source '{source}'. Must be one of: {', '.join(sorted(_VALID_SOURCES))}",
        }

    script = os.path.join(REPO_ROOT, "scripts", "lightrag_ingest.py")
    cmd = [PYTHON, script, "--source", source]
    if force:
        cmd.append("--force")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return {
            "success": proc.returncode == 0,
            "output": output.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "Ingest timed out after 120 seconds"}
    except FileNotFoundError:
        return {"success": False, "output": f"Script not found: {script}"}
    except Exception as ex:
        return {"success": False, "output": str(ex)}


def _handle_lightrag_query(argv):
    """
    lightrag_query "why was reb removed from stat whitelist"

    Sends a hybrid query to the LightRAG server and returns the response.
    """
    if len(argv) < 3:
        return {
            "success": False,
            "error": "Missing query text. Usage: lightrag_query \"your question here\"",
        }

    text = " ".join(argv[2:])
    base_url = _get_base_url()

    try:
        import requests
    except ImportError:
        return {"success": False, "error": "requests library not installed"}

    try:
        resp = requests.post(
            f"{base_url}/query",
            json={"query": text, "mode": "hybrid"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        response_text = data if isinstance(data, str) else data.get("response", str(data))
        return {
            "success": True,
            "query": text,
            "response": response_text,
        }
    except requests.ConnectionError:
        return {"success": False, "error": f"Cannot connect to LightRAG at {base_url}"}
    except requests.Timeout:
        return {"success": False, "error": "LightRAG query timed out (30s)"}
    except requests.HTTPError as ex:
        return {"success": False, "error": f"HTTP {ex.response.status_code}: {ex.response.text}"}
    except Exception as ex:
        return {"success": False, "error": str(ex)}


def _handle_lightrag_health(argv):
    """
    lightrag_health

    Checks whether the LightRAG server is reachable and healthy.
    """
    base_url = _get_base_url()

    try:
        import requests
    except ImportError:
        return {"success": False, "error": "requests library not installed"}

    try:
        resp = requests.get(f"{base_url}/health", timeout=10)
        resp.raise_for_status()
        return {
            "success": True,
            "status": "healthy",
            "url": base_url,
        }
    except requests.ConnectionError:
        return {"success": False, "error": f"Cannot connect to LightRAG at {base_url}"}
    except requests.Timeout:
        return {"success": False, "error": "Health check timed out (10s)"}
    except requests.HTTPError as ex:
        return {"success": False, "error": f"HTTP {ex.response.status_code}: {ex.response.text}"}
    except Exception as ex:
        return {"success": False, "error": str(ex)}


_COMMANDS = {
    "lightrag_ingest": _handle_lightrag_ingest,
    "lightrag_query": _handle_lightrag_query,
    "lightrag_health": _handle_lightrag_health,
}
