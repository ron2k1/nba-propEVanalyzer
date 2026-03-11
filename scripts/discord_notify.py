#!/usr/bin/env python3
"""
Discord webhook notification module for scheduled NBA pipeline tasks.

Sends structured rich embeds to a Discord channel via webhook URL.
Supports: morning summary, evening picks, failure alerts, dead-man alerts,
dense collector, line movement, injury alerts.

Usage
-----
    from scripts.discord_notify import (
        notify_morning_summary,
        notify_evening_picks,
        notify_failure,
        notify_deadman,
        notify_dense_collector,
        notify_line_movement,
        notify_injury_alert,
        send_test,
    )

Requires DISCORD_WEBHOOK_URL in .env (or environment).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

_log = logging.getLogger("discord_notify")

# ---------------------------------------------------------------------------
# Webhook transport
# ---------------------------------------------------------------------------

def _get_webhook_url() -> str | None:
    return os.getenv("DISCORD_WEBHOOK_URL")


def send_webhook(payload: dict) -> dict:
    """
    POST a Discord webhook payload (embeds, content, etc).

    Returns {"success": True} or {"success": False, "error": "..."}.
    """
    url = _get_webhook_url()
    if not url:
        return {"success": False, "error": "DISCORD_WEBHOOK_URL not set"}

    body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "NbaPipeline/1.0")

    try:
        with urlopen(req, timeout=15) as resp:
            status = resp.status
            # Discord returns 204 No Content on success
            if status in (200, 204):
                return {"success": True, "status": status}
            return {"success": False, "error": f"HTTP {status}"}
    except HTTPError as exc:
        return {"success": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except URLError as exc:
        return {"success": False, "error": f"URL error: {exc.reason}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Embed helpers
# ---------------------------------------------------------------------------

COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_ORANGE = 0xF39C12
COLOR_BLUE = 0x3498DB

def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _build_embed(
    title: str,
    description: str = "",
    color: int = COLOR_BLUE,
    fields: list[dict] | None = None,
    footer: str | None = None,
) -> dict:
    embed: dict[str, Any] = {
        "title": title,
        "color": color,
        "timestamp": _utc_now_iso(),
    }
    if description:
        embed["description"] = description[:4096]
    if fields:
        embed["fields"] = fields[:25]  # Discord limit
    if footer:
        embed["footer"] = {"text": footer[:2048]}
    return embed


def _fmt_pct(val: float | None, decimals: int = 1) -> str:
    if val is None:
        return "—"
    return f"{val * 100 if abs(val) < 1 else val:+.{decimals}f}%"


def _fmt_num(val: float | int | None, decimals: int = 2) -> str:
    if val is None:
        return "—"
    return f"{val:.{decimals}f}"


# ---------------------------------------------------------------------------
# Notification types
# ---------------------------------------------------------------------------

def notify_morning_summary(settle_payload: dict) -> dict:
    """
    Send morning settlement summary embed.

    settle_payload: the full JSON from scheduled_settle.py (steps include
    paper_settle result and paper_summary result with report + gate).
    """
    ok = settle_payload.get("success", False)
    duration = settle_payload.get("durationSec", 0)
    steps = settle_payload.get("steps", [])

    # Extract paper_summary result (second step)
    summary_result = {}
    settle_result = {}
    for step in steps:
        name = step.get("name", "")
        if name == "paper_summary" and step.get("result"):
            summary_result = step["result"]
        elif name == "paper_settle" and step.get("result"):
            settle_result = step["result"]

    gate = summary_result.get("gate", {})
    metrics = gate.get("metrics", {})
    gate_pass = gate.get("gatePass", False)

    # Settlement counts
    jsonl_j = settle_result.get("jsonlJournal", {})
    dj_res = settle_result.get("decisionJournal", {})
    leans_res = settle_result.get("leans", {})

    fields = []

    # Gate status
    fields.append({
        "name": "GO-LIVE Gate",
        "value": ("PASS" if gate_pass else "FAIL") + (
            f" — {gate.get('reason', '')}" if not gate_pass else ""
        ),
        "inline": False,
    })

    # Core metrics
    if metrics:
        fields.append({
            "name": "Sample",
            "value": str(metrics.get("sample", 0)),
            "inline": True,
        })
        fields.append({
            "name": "Hit Rate",
            "value": _fmt_pct(metrics.get("hit_rate")),
            "inline": True,
        })
        fields.append({
            "name": "ROI",
            "value": _fmt_pct(metrics.get("roi")),
            "inline": True,
        })
        fields.append({
            "name": "+CLV %",
            "value": _fmt_pct(metrics.get("positive_clv_pct"), 0),
            "inline": True,
        })

    # Settlement counts
    settled_total = (
        (jsonl_j.get("settledCount", 0) if isinstance(jsonl_j, dict) else 0)
        + (dj_res.get("settledCount", 0) if isinstance(dj_res, dict) else 0)
    )
    if settled_total > 0:
        fields.append({
            "name": "Settled Today",
            "value": str(settled_total),
            "inline": True,
        })

    # Model leans
    model_leans = gate.get("model_leans", {})
    if model_leans.get("sample"):
        fields.append({
            "name": "Model Leans",
            "value": (
                f"{model_leans['sample']} signals · "
                f"{_fmt_pct(model_leans.get('hitRate'))} hit · "
                f"{_fmt_pct(model_leans.get('roi'))} ROI"
            ),
            "inline": False,
        })

    color = COLOR_GREEN if ok and gate_pass else COLOR_ORANGE if ok else COLOR_RED
    title = "Morning Settlement" + (" — All Clear" if ok else " — Issues Found")

    embed = _build_embed(
        title=title,
        color=color,
        fields=fields,
        footer=f"Duration: {duration:.1f}s | Window: {gate.get('windowDays', 14)}d",
    )
    return send_webhook({"embeds": [embed]})


def notify_evening_picks(pipeline_payload: dict) -> dict:
    """
    Send evening picks embed after full_pipeline completes.

    pipeline_payload: the full JSON from scheduled_pipeline.py (steps include
    best_today result).
    """
    ok = pipeline_payload.get("success", False)
    duration = pipeline_payload.get("durationSec", 0)
    steps = pipeline_payload.get("steps", [])

    best_result = {}
    collect_result = {}
    for step in steps:
        name = step.get("name", "")
        if name == "best_today" and step.get("result"):
            best_result = step["result"]
        elif name == "collect_lines" and step.get("result"):
            collect_result = step["result"]

    top_plays = best_result.get("topPlays", [])
    policy_qualified = [p for p in top_plays if p.get("policyQualified")]

    # Build picks description
    lines = []
    for i, p in enumerate(policy_qualified[:8], 1):
        stat = str(p.get("stat", "")).upper()
        side = str(p.get("recommendedSide", "")).upper()
        ev = p.get("recommendedEvPct")
        ev_str = _fmt_pct(ev / 100 if ev and abs(ev) > 1 else ev) if ev else "—"
        proj = _fmt_num(p.get("projection"), 1)
        line_val = _fmt_num(p.get("line"), 1)
        book = ""
        if side == "OVER" and p.get("bestOverBook"):
            book = f" ({p['bestOverBook']})"
        elif side == "UNDER" and p.get("bestUnderBook"):
            book = f" ({p['bestUnderBook']})"

        lines.append(
            f"**{i}. {p.get('playerName', '?')}** — {stat} {side} {line_val}"
            f" · proj {proj} · EV {ev_str}{book}"
        )

    if not lines:
        lines.append("No policy-qualified picks found.")

    # Also show count of blocked leans
    blocked = [p for p in top_plays if not p.get("policyQualified")]

    fields = []
    if collect_result:
        snap_count = collect_result.get("totalSnapshots", collect_result.get("linesCollected", 0))
        if snap_count:
            fields.append({
                "name": "Lines Collected",
                "value": str(snap_count),
                "inline": True,
            })

    fields.append({
        "name": "Qualified Picks",
        "value": str(len(policy_qualified)),
        "inline": True,
    })
    fields.append({
        "name": "Blocked Leans",
        "value": str(len(blocked)),
        "inline": True,
    })

    color = COLOR_GREEN if ok and policy_qualified else COLOR_ORANGE if ok else COLOR_RED
    title = f"Evening Picks — {len(policy_qualified)} Plays" if ok else "Evening Pipeline — Failed"

    embed = _build_embed(
        title=title,
        description="\n".join(lines),
        color=color,
        fields=fields,
        footer=f"Duration: {duration:.1f}s | Total scanned: {len(top_plays)}",
    )
    return send_webhook({"embeds": [embed]})


def notify_collect_only(pipeline_payload: dict) -> dict:
    """
    Send a brief notification after a collect_only snapshot run.
    Only sends on failure (success is silent for high-frequency tasks).
    """
    ok = pipeline_payload.get("success", False)
    if ok:
        return {"success": True, "skipped": True, "reason": "collect_only success is silent"}

    steps = pipeline_payload.get("steps", [])
    failed_step = next((s for s in steps if not s.get("success")), {})

    embed = _build_embed(
        title="Snapshot Collection Failed",
        description=f"**Step:** {failed_step.get('name', '?')}\n**Error:** {failed_step.get('error', 'unknown')[:500]}",
        color=COLOR_RED,
        footer=f"Error class: {failed_step.get('errorClass', 'unknown')}",
    )
    return send_webhook({"embeds": [embed]})


def notify_failure(task_name: str, error: str, error_class: str = "unknown") -> dict:
    """Send a failure alert for any task."""
    embed = _build_embed(
        title=f"Task Failed — {task_name}",
        description=f"```\n{error[:1500]}\n```",
        color=COLOR_RED,
        fields=[
            {"name": "Error Class", "value": error_class, "inline": True},
            {"name": "Task", "value": task_name, "inline": True},
        ],
        footer="Check data/logs/scheduled_runs.jsonl for details",
    )
    return send_webhook({"embeds": [embed]})


def notify_deadman(health: dict) -> dict:
    """
    Send a dead-man alert when ops_health detects stale tasks.

    health: the output from read_ops_health().
    """
    tasks = health.get("tasks", {})
    stale_tasks = {k: v for k, v in tasks.items() if v.get("stale")}
    if not stale_tasks:
        return {"success": True, "skipped": True, "reason": "no stale tasks"}

    fields = []
    for name, td in stale_tasks.items():
        last_ok = td.get("lastSuccess") or "never"
        threshold = td.get("staleThresholdHours", "?")
        fields.append({
            "name": name,
            "value": f"Last success: {last_ok}\nThreshold: {threshold}h",
            "inline": True,
        })

    embed = _build_embed(
        title=f"Dead-Man Alert — {len(stale_tasks)} Stale Task(s)",
        description="One or more scheduled tasks have not succeeded within their expected interval.",
        color=COLOR_ORANGE,
        fields=fields,
        footer=f"Checked at {health.get('checkedAtUtc', '?')}",
    )
    return send_webhook({"embeds": [embed]})


def notify_dense_collector(result: dict) -> dict:
    """
    Send dense collector completion/failure embed.

    result: the final JSON output from dense_collector.py.
    """
    ok = result.get("success", False)
    events = result.get("events", 0)
    windows = result.get("windows_completed", 0)
    total_windows = result.get("total_windows", 0)
    api_calls = result.get("total_api_calls", 0)
    snaps = result.get("total_snapshots", 0)
    bridge = result.get("bridge_and_build")

    fields = [
        {"name": "Events", "value": str(events), "inline": True},
        {"name": "Windows", "value": f"{windows}/{total_windows}", "inline": True},
        {"name": "API Calls", "value": str(api_calls), "inline": True},
        {"name": "Snapshots", "value": str(snaps), "inline": True},
    ]

    if bridge and isinstance(bridge, dict) and not bridge.get("error"):
        b = bridge.get("bridge", {})
        c = bridge.get("build_closes", {})
        fields.append({
            "name": "Bridge+Build",
            "value": f"Inserted {b.get('inserted', 0)} | Closes {c.get('saved', 0)}",
            "inline": False,
        })
    elif bridge and isinstance(bridge, dict) and bridge.get("error"):
        fields.append({
            "name": "Bridge+Build",
            "value": f"Failed: {str(bridge['error'])[:100]}",
            "inline": False,
        })

    color = COLOR_GREEN if ok else COLOR_RED
    title = "Dense Collector — Complete" if ok else "Dense Collector — Failed"

    embed = _build_embed(
        title=title,
        color=color,
        fields=fields,
        footer=f"Snapshots per window: {round(snaps / max(windows, 1))}",
    )
    return send_webhook({"embeds": [embed]})


COLOR_PURPLE = 0x9B59B6


def notify_line_movement(movements: list[dict]) -> dict:
    """
    Send line movement alert embed.

    movements: list of detected line/odds changes from monitor_lines.py.
    """
    if not movements:
        return {"success": True, "skipped": True, "reason": "no movements"}

    line_moves = [m for m in movements if m["type"] == "line_move"]
    odds_shifts = [m for m in movements if m["type"] == "odds_shift"]

    lines = []
    for m in line_moves[:10]:
        arrow = "^" if m["delta"] > 0 else "v"
        lines.append(
            f"**{m['player']}** {m['stat'].upper()} "
            f"{m['prevLine']} -> {m['curLine']} ({m['direction']} {abs(m['delta'])})"
        )

    for m in odds_shifts[:5]:
        lines.append(
            f"**{m['player']}** {m['stat'].upper()} {m['side']} odds "
            f"{m['prevOdds']} -> {m['curOdds']}"
        )

    embed = _build_embed(
        title=f"Line Movement — {len(line_moves)} Line(s), {len(odds_shifts)} Odds Shift(s)",
        description="\n".join(lines) if lines else "No details",
        color=COLOR_PURPLE,
        footer=f"Total movements detected: {len(movements)}",
    )
    return send_webhook({"embeds": [embed]})


COLOR_YELLOW = 0xF1C40F


def notify_injury_alert(signals: list[dict]) -> dict:
    """
    Send injury alert embed for new high-confidence signals.

    signals: list of new injury signals from monitor_injuries.py.
    """
    if not signals:
        return {"success": True, "skipped": True, "reason": "no new signals"}

    lines = []
    for sig in signals[:12]:
        player = sig.get("player", "?")
        status = sig.get("status", "?")
        team = sig.get("team", "?")
        confidence = sig.get("confidence", 0)
        source = sig.get("source", "")
        lines.append(
            f"**{player}** ({team}) — {status} "
            f"[{confidence:.0%} conf{', ' + source if source else ''}]"
        )

    embed = _build_embed(
        title=f"Injury Alert — {len(signals)} New Signal(s)",
        description="\n".join(lines),
        color=COLOR_YELLOW,
        fields=[
            {"name": "Impact", "value": "Check today's picks for affected players", "inline": False},
        ],
        footer=f"Signals above confidence threshold",
    )
    return send_webhook({"embeds": [embed]})


def send_test() -> dict:
    """Send a test embed to verify webhook configuration."""
    embed = _build_embed(
        title="NBA Pipeline — Webhook Test",
        description="If you see this, Discord notifications are working.",
        color=COLOR_BLUE,
        fields=[
            {"name": "Status", "value": "Connected", "inline": True},
            {"name": "Source", "value": "scripts/discord_notify.py", "inline": True},
        ],
        footer="Sent from /api/discord_test",
    )
    return send_webhook({"embeds": [embed]})
