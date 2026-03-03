#!/usr/bin/env python3
"""Thin LightRAG client for optional RAG-enriched prompts."""

import os
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

_LIGHTRAG_BASE = os.getenv("LIGHTRAG_BASE_URL", "http://localhost:9621").rstrip("/")
_LIGHTRAG_ENABLED = os.getenv("LIGHTRAG_ENABLED", "false").strip().lower() in ("true", "1", "yes")
_TIMEOUT = 10


def query_rag(question, mode="hybrid"):
    """
    Query LightRAG for contextual information.

    Returns the response text if successful, None if disabled/offline/error.
    Mode: "hybrid" (default), "naive", "local", "global"
    """
    if not _LIGHTRAG_ENABLED:
        return None
    try:
        resp = requests.post(
            f"{_LIGHTRAG_BASE}/query",
            json={"query": question, "mode": mode},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("response") or data.get("result") or str(data)
        return None
    except Exception:
        return None


def is_available():
    """Check if LightRAG is enabled and reachable."""
    if not _LIGHTRAG_ENABLED:
        return False
    try:
        resp = requests.get(f"{_LIGHTRAG_BASE}/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False
