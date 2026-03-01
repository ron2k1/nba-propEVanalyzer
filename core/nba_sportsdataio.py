#!/usr/bin/env python3
"""Helpers for SportsDataIO NBA feed access."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

import requests

SPORTSDATAIO_BASE_URL = "https://api.sportsdata.io/api/nba"


def to_sportsdataio_date(date_str: str) -> str:
    """
    Convert YYYY-MM-DD into SportsDataIO date token (YYYY-MMM-DD).
    Example: 2026-02-27 -> 2026-FEB-27
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%Y-%b-%d").upper()


def iter_dates(date_from: str, date_to: str):
    """Yield YYYY-MM-DD dates in inclusive range."""
    cur = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    while cur <= end:
        yield cur.isoformat()
        cur += timedelta(days=1)


def extract_game_ids(games_payload: Any) -> list[int]:
    """Best-effort extraction of game IDs from a games-by-date response."""
    items: list[Any]
    if isinstance(games_payload, list):
        items = games_payload
    elif isinstance(games_payload, dict):
        for key in ("Games", "games", "Data", "data", "items"):
            val = games_payload.get(key)
            if isinstance(val, list):
                items = val
                break
        else:
            items = []
    else:
        items = []

    out: list[int] = []
    seen: set[int] = set()
    for row in items:
        if not isinstance(row, dict):
            continue
        raw = None
        for key in ("GameID", "GameId", "gameId", "ID", "Id", "id"):
            if key in row and row.get(key) is not None:
                raw = row.get(key)
                break
        if raw is None:
            continue
        try:
            gid = int(raw)
        except (TypeError, ValueError):
            continue
        if gid in seen:
            continue
        seen.add(gid)
        out.append(gid)
    return out


class SportsDataIOClient:
    """Thin HTTP client for SportsDataIO NBA feeds."""

    def __init__(self, api_key: str | None = None, timeout_sec: int = 30):
        key = (api_key or os.getenv("SPORTSDATA_API_KEY", "")).strip()
        if not key:
            raise ValueError("SPORTSDATA_API_KEY is missing")
        self.api_key = key
        self.timeout_sec = timeout_sec

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        GET a SportsDataIO endpoint path.
        Returns dict with keys:
          success: bool
          statusCode: int | None
          data: Any
          error: str | None
          url: str
        """
        url = f"{SPORTSDATAIO_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        query = dict(params or {})
        query.setdefault("key", self.api_key)
        headers = {"Ocp-Apim-Subscription-Key": self.api_key}
        try:
            resp = requests.get(url, params=query, headers=headers, timeout=self.timeout_sec)
        except Exception as e:
            return {
                "success": False,
                "statusCode": None,
                "data": None,
                "error": str(e),
                "url": url,
            }

        if resp.status_code != 200:
            snippet = (resp.text or "")[:500]
            return {
                "success": False,
                "statusCode": resp.status_code,
                "data": None,
                "error": f"SportsDataIO HTTP {resp.status_code}: {snippet}",
                "url": url,
            }

        try:
            payload = resp.json()
        except Exception as e:
            return {
                "success": False,
                "statusCode": resp.status_code,
                "data": None,
                "error": f"Invalid JSON response: {e}",
                "url": url,
            }

        return {
            "success": True,
            "statusCode": resp.status_code,
            "data": payload,
            "error": None,
            "url": url,
        }
