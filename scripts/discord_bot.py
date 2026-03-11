#!/usr/bin/env python3
"""
Discord bot with slash commands for the NBA prop pipeline.

Exposes four slash commands that query the local API server:
  /picks   — today's top policy-qualified plays
  /gate    — current GO-LIVE gate status
  /summary — paper trading summary (14d window)
  /health  — ops health / task staleness

Requires:
  pip install discord.py
  DISCORD_BOT_TOKEN in .env
  DISCORD_CHANNEL_ID in .env (optional — restricts commands to one channel)
  Server running on API_BASE_URL (default http://127.0.0.1:8787)

Usage:
  .venv/Scripts/python.exe scripts/discord_bot.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

API_BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8787")
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "")  # optional restriction
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")  # set for instant slash command sync
LEANS_CHANNEL_ID = os.getenv("DISCORD_LEANS_CHANNEL_ID", "")  # auto-post model leans here
PICKS_CHANNEL_ID = os.getenv("DISCORD_PICKS_CHANNEL_ID", "")  # send picks to this channel
OWNER_ID = os.getenv("DISCORD_OWNER_ID", "")  # restrict commands to this user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
_log = logging.getLogger("discord_bot")

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api_get(path: str) -> dict:
    """GET a JSON endpoint from the local API server."""
    url = f"{API_BASE}{path}"
    req = Request(url, method="GET")
    req.add_header("User-Agent", "NbaDiscordBot/1.0")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        return {"success": False, "error": f"HTTP {exc.code}: {body}"}
    except URLError as exc:
        return {"success": False, "error": f"Connection failed: {exc.reason}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_ORANGE = 0xF39C12
COLOR_BLUE = 0x3498DB


def _fmt_pct(val, decimals: int = 1) -> str:
    if val is None:
        return "—"
    v = float(val)
    return f"{v * 100 if abs(v) < 1 else v:+.{decimals}f}%"


def _fmt_num(val, decimals: int = 1) -> str:
    if val is None:
        return "—"
    return f"{float(val):.{decimals}f}"


def _error_embed(title: str, error: str):
    """Build a red error embed (returns a discord.Embed)."""
    import discord
    return discord.Embed(
        title=title,
        description=f"```\n{error[:1500]}\n```",
        color=COLOR_RED,
        timestamp=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# /picks command
# ---------------------------------------------------------------------------

def build_picks_embed(data: dict):
    """Build embed from /api/best_today response."""
    import discord

    if not data.get("success"):
        return _error_embed("Picks — Error", data.get("error", "Unknown error"))

    # API returns separate lists: policyQualified, topOffers, modelLeans
    qualified = data.get("policyQualified", [])
    model_leans = data.get("modelLeans", [])
    total_scanned = data.get("totalRanked", len(qualified) + len(model_leans))

    lines = []
    for i, p in enumerate(qualified, 1):
        stat = str(p.get("stat", "")).upper()
        side = str(p.get("recommendedSide", "")).upper()
        ev = p.get("recommendedEvPct")
        ev_str = _fmt_pct(ev / 100 if ev and abs(ev) > 1 else ev) if ev else "—"
        proj = _fmt_num(p.get("projection"))
        line_val = _fmt_num(p.get("line"))
        book = ""
        if side == "OVER" and p.get("bestOverBook"):
            book = f" ({p['bestOverBook']})"
        elif side == "UNDER" and p.get("bestUnderBook"):
            book = f" ({p['bestUnderBook']})"
        lines.append(
            f"**{i}. {p.get('playerName', '?')}** — {stat} {side} {line_val}"
            f" | proj {proj} | EV {ev_str}{book}"
        )

    if not lines:
        lines.append("No policy-qualified picks found.")

    embed = discord.Embed(
        title=f"Today's Picks — {len(qualified)} Plays",
        description="\n".join(lines)[:4096],
        color=COLOR_GREEN if qualified else COLOR_ORANGE,
        timestamp=datetime.now(UTC),
    )
    embed.add_field(name="Qualified", value=str(len(qualified)), inline=True)
    embed.add_field(name="Model Leans", value=str(len(model_leans)), inline=True)
    embed.add_field(name="Total Scanned", value=str(total_scanned), inline=True)
    embed.set_footer(text="Source: /api/best_today")
    return embed


def build_leans_embed(data: dict):
    """Build embed for model leans from /api/best_today response."""
    import discord

    model_leans = data.get("modelLeans", [])
    if not model_leans:
        return discord.Embed(
            title="Model Leans — 0 Signals",
            description="No model leans found.",
            color=COLOR_ORANGE,
            timestamp=datetime.now(UTC),
        )

    lines = []
    for i, p in enumerate(model_leans, 1):
        stat = str(p.get("stat", "")).upper()
        side = str(p.get("recommendedSide", "")).upper()
        ev = p.get("recommendedEvPct")
        ev_str = _fmt_pct(ev / 100 if ev and abs(ev) > 1 else ev) if ev else "—"
        proj = _fmt_num(p.get("projection"))
        line_val = _fmt_num(p.get("line"))
        conf = p.get("confidence")
        conf_str = f"{conf:.0%}" if conf is not None else "—"
        lines.append(
            f"**{i}. {p.get('playerName', '?')}** — {stat} {side} {line_val}"
            f" | proj {proj} | EV {ev_str} | conf {conf_str}"
        )

    embed = discord.Embed(
        title=f"Model Leans — {len(model_leans)} Signals",
        description="\n".join(lines)[:4096],
        color=COLOR_BLUE,
        timestamp=datetime.now(UTC),
    )
    embed.set_footer(text="Research only — not policy-qualified | Source: /api/best_today")
    return embed


# ---------------------------------------------------------------------------
# /gate command
# ---------------------------------------------------------------------------

def build_gate_embed(data: dict):
    """Build embed from /api/journal_gate response."""
    import discord

    if not data.get("success"):
        return _error_embed("Gate — Error", data.get("error", "Unknown error"))

    gate = data.get("gate", data)
    gate_pass = gate.get("gatePass", False)
    metrics = gate.get("metrics", {})
    model_leans = gate.get("model_leans", {})

    color = COLOR_GREEN if gate_pass else COLOR_RED
    status = "PASS" if gate_pass else "FAIL"
    reason = gate.get("reason", "")

    embed = discord.Embed(
        title=f"GO-LIVE Gate — {status}",
        description=reason if not gate_pass else "All gate criteria met.",
        color=color,
        timestamp=datetime.now(UTC),
    )

    if metrics:
        embed.add_field(name="Sample", value=str(metrics.get("sample", 0)), inline=True)
        embed.add_field(name="Hit Rate", value=_fmt_pct(metrics.get("hit_rate")), inline=True)
        embed.add_field(name="ROI", value=_fmt_pct(metrics.get("roi")), inline=True)
        embed.add_field(name="+CLV %", value=_fmt_pct(metrics.get("positive_clv_pct"), 0), inline=True)

    window = gate.get("windowDays", 14)
    embed.add_field(name="Window", value=f"{window}d", inline=True)

    if model_leans and model_leans.get("sample"):
        embed.add_field(
            name="Model Leans",
            value=(
                f"{model_leans['sample']} signals | "
                f"{_fmt_pct(model_leans.get('hitRate'))} hit | "
                f"{_fmt_pct(model_leans.get('roi'))} ROI"
            ),
            inline=False,
        )

    embed.set_footer(text="Source: /api/journal_gate")
    return embed


# ---------------------------------------------------------------------------
# /summary command
# ---------------------------------------------------------------------------

def build_summary_embed(data: dict):
    """Build embed from /api/paper_summary response."""
    import discord

    if not data.get("success"):
        return _error_embed("Summary — Error", data.get("error", "Unknown error"))

    report = data.get("report", {})
    gate = data.get("gate", {})
    metrics = gate.get("metrics", {})

    gate_pass = gate.get("gatePass", False)
    color = COLOR_GREEN if gate_pass else COLOR_ORANGE

    embed = discord.Embed(
        title="Paper Trading Summary",
        color=color,
        timestamp=datetime.now(UTC),
    )

    # Gate status
    gate_str = "PASS" if gate_pass else f"FAIL — {gate.get('reason', '')}"
    embed.add_field(name="GO-LIVE Gate", value=gate_str, inline=False)

    # Core metrics
    if metrics:
        embed.add_field(name="Sample", value=str(metrics.get("sample", 0)), inline=True)
        embed.add_field(name="Hit Rate", value=_fmt_pct(metrics.get("hit_rate")), inline=True)
        embed.add_field(name="ROI", value=_fmt_pct(metrics.get("roi")), inline=True)
        embed.add_field(name="+CLV %", value=_fmt_pct(metrics.get("positive_clv_pct"), 0), inline=True)

    # Per-stat breakdown from report
    stat_rows = report.get("by_stat", [])
    if stat_rows:
        stat_lines = []
        for row in stat_rows[:6]:
            stat = str(row.get("stat", "?")).upper()
            n = row.get("sample", 0)
            hr = _fmt_pct(row.get("hit_rate"))
            roi = _fmt_pct(row.get("roi"))
            stat_lines.append(f"`{stat:6s}` {n:3d} bets | {hr} hit | {roi} ROI")
        embed.add_field(
            name="By Stat",
            value="\n".join(stat_lines),
            inline=False,
        )

    # Model leans
    model_leans = gate.get("model_leans", {})
    if model_leans and model_leans.get("sample"):
        embed.add_field(
            name="Model Leans",
            value=(
                f"{model_leans['sample']} signals | "
                f"{_fmt_pct(model_leans.get('hitRate'))} hit | "
                f"{_fmt_pct(model_leans.get('roi'))} ROI"
            ),
            inline=False,
        )

    window = gate.get("windowDays", 14)
    embed.set_footer(text=f"Window: {window}d | Source: /api/paper_summary")
    return embed


# ---------------------------------------------------------------------------
# /health command
# ---------------------------------------------------------------------------

def build_health_embed(data: dict):
    """Build embed from /api/ops_health response."""
    import discord

    if not data.get("success"):
        return _error_embed("Health — Error", data.get("error", "Unknown error"))

    healthy = data.get("healthy", False)
    tasks = data.get("tasks", {})

    color = COLOR_GREEN if healthy else COLOR_RED
    embed = discord.Embed(
        title=f"Ops Health — {'Healthy' if healthy else 'Issues Detected'}",
        color=color,
        timestamp=datetime.now(UTC),
    )

    for name, info in tasks.items():
        stale = info.get("stale", False)
        last_ok = info.get("lastSuccess") or "never"
        last_fail = info.get("lastFailure")
        runs = info.get("runsLast24h", 0)
        fails = info.get("failuresLast24h", 0)

        status = "STALE" if stale else "OK"
        lines = [f"Status: **{status}**", f"Last OK: {last_ok}"]
        if last_fail:
            lines.append(f"Last fail: {last_fail}")
        lines.append(f"24h: {runs} runs, {fails} failures")

        embed.add_field(name=name, value="\n".join(lines), inline=True)

    if not tasks:
        embed.description = "No task history found in logs."

    embed.set_footer(text=f"Checked: {data.get('checkedAtUtc', '?')} | Source: /api/ops_health")
    return embed


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

def main() -> int:
    if not BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not set in .env", file=sys.stderr)
        return 1

    try:
        import discord
        from discord import app_commands
    except ImportError:
        print("ERROR: discord.py not installed. Run:", file=sys.stderr)
        print("  .venv\\Scripts\\python.exe -m pip install discord.py", file=sys.stderr)
        return 1

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    def _access_check(interaction: discord.Interaction) -> bool:
        """Check owner and channel restrictions."""
        if OWNER_ID and str(interaction.user.id) != OWNER_ID:
            return False
        if CHANNEL_ID and str(interaction.channel_id) != CHANNEL_ID:
            return False
        return True

    # -- /picks ---------------------------------------------------------------
    @tree.command(name="picks", description="Today's top policy-qualified plays")
    async def cmd_picks(interaction: discord.Interaction):
        if not _access_check(interaction):
            await interaction.response.send_message(
                "This command is restricted to a specific channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        data = _api_get("/api/best_today?limit=15")

        if not data.get("success"):
            await interaction.followup.send(
                f"API error: {data.get('error', 'unknown')}", ephemeral=True
            )
            return

        posted = []

        # Send picks to the picks channel
        if PICKS_CHANNEL_ID:
            try:
                picks_ch = client.get_channel(int(PICKS_CHANNEL_ID))
                if picks_ch:
                    picks_embed = build_picks_embed(data)
                    await picks_ch.send(embed=picks_embed)
                    posted.append(f"<#{PICKS_CHANNEL_ID}>")
            except Exception as exc:
                _log.warning(f"Failed to post picks: {exc}")

        # Send model leans to the leans channel
        if LEANS_CHANNEL_ID and data.get("modelLeans"):
            try:
                leans_ch = client.get_channel(int(LEANS_CHANNEL_ID))
                if leans_ch:
                    leans_embed = build_leans_embed(data)
                    await leans_ch.send(embed=leans_embed)
                    posted.append(f"<#{LEANS_CHANNEL_ID}>")
            except Exception as exc:
                _log.warning(f"Failed to post leans: {exc}")

        qualified = data.get("policyQualified", [])
        leans = data.get("modelLeans", [])
        where = ", ".join(posted) if posted else "nowhere (no channel IDs set)"
        await interaction.followup.send(
            f"Posted {len(qualified)} picks + {len(leans)} leans to {where}",
            ephemeral=True,
        )

    # -- /gate ----------------------------------------------------------------
    @tree.command(name="gate", description="Current GO-LIVE gate status")
    async def cmd_gate(interaction: discord.Interaction):
        if not _access_check(interaction):
            await interaction.response.send_message(
                "This command is restricted to a specific channel.", ephemeral=True
            )
            return
        await interaction.response.defer()
        data = _api_get("/api/journal_gate?windowDays=14")
        embed = build_gate_embed(data)
        await interaction.followup.send(embed=embed)

    # -- /summary -------------------------------------------------------------
    @tree.command(name="summary", description="Paper trading summary (14d window)")
    async def cmd_summary(interaction: discord.Interaction):
        if not _access_check(interaction):
            await interaction.response.send_message(
                "This command is restricted to a specific channel.", ephemeral=True
            )
            return
        await interaction.response.defer()
        data = _api_get("/api/paper_summary?windowDays=14")
        embed = build_summary_embed(data)
        await interaction.followup.send(embed=embed)

    # -- /health --------------------------------------------------------------
    @tree.command(name="health", description="Ops health and task staleness")
    async def cmd_health(interaction: discord.Interaction):
        if not _access_check(interaction):
            await interaction.response.send_message(
                "This command is restricted to a specific channel.", ephemeral=True
            )
            return
        await interaction.response.defer()
        data = _api_get("/api/ops_health")
        embed = build_health_embed(data)
        await interaction.followup.send(embed=embed)

    # -- Events ---------------------------------------------------------------
    @client.event
    async def on_ready():
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            # Copy commands to guild first, then clear global duplicates
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            tree.clear_commands(guild=None)
            await tree.sync()  # pushes empty global list to remove duplicates
            _log.info(f"Bot ready as {client.user} — synced {len(synced)} commands to guild {GUILD_ID} (global cleared)")
        else:
            await tree.sync()
            _log.info(f"Bot ready as {client.user} — synced {len(tree.get_commands())} commands globally (may take up to 1h)")
        if CHANNEL_ID:
            _log.info(f"Commands restricted to channel {CHANNEL_ID}")
        else:
            _log.info("Commands available in all channels")

    _log.info("Starting Discord bot...")
    client.run(BOT_TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
