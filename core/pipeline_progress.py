#!/usr/bin/env python3
"""Shared helpers for pipeline progress/status files."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_status(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def write_status(path: str | None, payload: dict) -> None:
    if not path:
        return

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    data = dict(payload or {})
    data.setdefault("updatedAtUtc", utc_now_iso())

    fd, tmp_path = tempfile.mkstemp(prefix="pipeline_status_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=True, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def merge_status(path: str | None, **updates) -> dict:
    current = read_status(path)
    current.update({k: v for k, v in updates.items() if v is not None})
    current["updatedAtUtc"] = utc_now_iso()
    write_status(path, current)
    return current
