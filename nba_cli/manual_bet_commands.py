#!/usr/bin/env python3
"""Personal manual bet tracker — completely separate from model pipeline."""

import json
import os
import uuid
from datetime import date, datetime, timezone
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG  = os.path.join(_ROOT, "data", "manual_bets", "manual_bets.jsonl")


# ── helpers ──────────────────────────────────────────────────────────────────

def _ensure_dir():
    os.makedirs(os.path.dirname(_LOG), exist_ok=True)


def _load_all():
    if not os.path.exists(_LOG):
        return []
    with open(_LOG, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _save_all(bets):
    _ensure_dir()
    with open(_LOG, "w", encoding="utf-8") as f:
        for b in bets:
            f.write(json.dumps(b) + "\n")


def _append(bet):
    _ensure_dir()
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(bet) + "\n")


def _pnl_from_odds(odds, units=1.0):
    """Return profit in units for a WIN at given American odds."""
    if odds >= 0:
        return round(units * odds / 100, 4)
    else:
        return round(units * 100 / abs(odds), 4)


# ── commands ─────────────────────────────────────────────────────────────────

def _handle_add(argv):
    """manual_bet add <player> <stat> <side:over|under> <line> <odds> [--units N] [--date YYYY-MM-DD] [--notes "..."]"""
    args = argv[3:]
    if len(args) < 5:
        return {"error": "Usage: manual_bet add <player> <stat> <over|under> <line> <odds> [--units N] [--date YYYY-MM-DD] [--notes TEXT]"}

    player = args[0]
    stat   = args[1].lower()
    side   = args[2].lower()
    if side not in ("over", "under"):
        return {"error": f"side must be 'over' or 'under', got '{side}'"}

    try:
        line = float(args[3])
        odds = int(args[4])
    except ValueError:
        return {"error": "line must be a number, odds must be an integer"}

    units     = 1.0
    bet_date  = date.today().isoformat()
    notes     = ""

    i = 5
    while i < len(args):
        tok = args[i]
        if tok == "--units" and i + 1 < len(args):
            try: units = float(args[i + 1])
            except ValueError: pass
            i += 2
        elif tok == "--date" and i + 1 < len(args):
            bet_date = args[i + 1]
            i += 2
        elif tok == "--notes" and i + 1 < len(args):
            notes = args[i + 1]
            i += 2
        else:
            i += 1

    bet = {
        "bet_id":    str(uuid.uuid4()),
        "date":      bet_date,
        "logged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "player":    player,
        "stat":      stat,
        "line":      line,
        "side":      side,
        "odds":      odds,
        "units":     units,
        "result":    "pending",
        "actual":    None,
        "pnl":       None,
        "notes":     notes,
    }
    _append(bet)
    return {
        "success": True,
        "bet_id":  bet["bet_id"],
        "summary": f"{side.upper()} {player} {stat} {line} @ {odds:+d}  [{bet_date}]",
    }


def _handle_settle(argv):
    """manual_bet settle <player> <stat> <actual_value> [--date YYYY-MM-DD]"""
    args = argv[3:]
    if len(args) < 3:
        return {"error": "Usage: manual_bet settle <player> <stat> <actual_value> [--date YYYY-MM-DD]"}

    player = args[0]
    stat   = args[1].lower()
    try:
        actual = float(args[2])
    except ValueError:
        return {"error": "actual_value must be a number"}

    settle_date = None
    i = 3
    while i < len(args):
        if args[i] == "--date" and i + 1 < len(args):
            settle_date = args[i + 1]
            i += 2
        else:
            i += 1

    bets    = _load_all()
    updated = 0
    for b in bets:
        if b["result"] != "pending":
            continue
        if b["player"].lower() != player.lower():
            continue
        if b["stat"] != stat:
            continue
        if settle_date and b["date"] != settle_date:
            continue

        b["actual"] = actual
        if actual == b["line"]:
            b["result"] = "push"
            b["pnl"]    = 0.0
        elif (b["side"] == "over"  and actual > b["line"]) or \
             (b["side"] == "under" and actual < b["line"]):
            b["result"] = "win"
            b["pnl"]    = _pnl_from_odds(b["odds"], b["units"])
        else:
            b["result"] = "loss"
            b["pnl"]    = -b["units"]
        updated += 1

    if not updated:
        return {"success": False, "error": f"No pending bet found for {player} {stat}" + (f" on {settle_date}" if settle_date else "")}

    _save_all(bets)
    settled = [b for b in bets if b["player"].lower() == player.lower()
               and b["stat"] == stat and b["result"] != "pending"][-1]
    return {
        "success": True,
        "updated": updated,
        "result":  settled["result"],
        "actual":  actual,
        "pnl":     settled["pnl"],
        "summary": f"{settled['side'].upper()} {player} {stat} {settled['line']} — actual {actual} → {settled['result'].upper()}  PnL: {settled['pnl']:+.3f}u",
    }


def _handle_list(argv):
    """manual_bet list [--date YYYY-MM-DD] [--pending] [--stat <stat>]"""
    args      = argv[3:]
    filt_date = None
    pending   = False
    filt_stat = None

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--date" and i + 1 < len(args):
            filt_date = args[i + 1]; i += 2
        elif tok == "--pending":
            pending = True; i += 1
        elif tok == "--stat" and i + 1 < len(args):
            filt_stat = args[i + 1].lower(); i += 2
        else:
            i += 1

    bets = _load_all()
    if filt_date: bets = [b for b in bets if b["date"] == filt_date]
    if pending:   bets = [b for b in bets if b["result"] == "pending"]
    if filt_stat: bets = [b for b in bets if b["stat"] == filt_stat]

    rows = []
    for b in sorted(bets, key=lambda x: x["date"], reverse=True):
        rows.append({
            "date":   b["date"],
            "player": b["player"],
            "stat":   b["stat"],
            "side":   b["side"],
            "line":   b["line"],
            "odds":   b["odds"],
            "units":  b["units"],
            "result": b["result"],
            "actual": b["actual"],
            "pnl":    b["pnl"],
        })

    return {"success": True, "count": len(rows), "bets": rows}


def _handle_summary(argv):
    """manual_bet summary [--window-days N] [--stat <stat>]"""
    args        = argv[3:]
    window_days = None
    filt_stat   = None

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--window-days" and i + 1 < len(args):
            try: window_days = int(args[i + 1])
            except ValueError: pass
            i += 2
        elif tok == "--stat" and i + 1 < len(args):
            filt_stat = args[i + 1].lower(); i += 2
        else:
            i += 1

    bets = _load_all()
    if window_days:
        from datetime import timedelta
        cutoff = (date.today() - timedelta(days=window_days)).isoformat()
        bets = [b for b in bets if b["date"] >= cutoff]
    if filt_stat:
        bets = [b for b in bets if b["stat"] == filt_stat]

    settled  = [b for b in bets if b["result"] in ("win", "loss", "push")]
    pending  = [b for b in bets if b["result"] == "pending"]
    wins     = [b for b in settled if b["result"] == "win"]
    losses   = [b for b in settled if b["result"] == "loss"]
    pushes   = [b for b in settled if b["result"] == "push"]
    total_pnl     = sum(b["pnl"] for b in settled)
    total_wagered = sum(b["units"] for b in settled)
    hit_rate      = round(len(wins) / len(settled) * 100, 1) if settled else None
    roi           = round(total_pnl / total_wagered * 100, 2) if total_wagered else None

    # Per-stat breakdown
    by_stat = defaultdict(lambda: {"bets": 0, "wins": 0, "pnl": 0.0, "wagered": 0.0})
    for b in settled:
        by_stat[b["stat"]]["bets"]    += 1
        by_stat[b["stat"]]["wagered"] += b["units"]
        by_stat[b["stat"]]["pnl"]     += b["pnl"]
        if b["result"] == "win":
            by_stat[b["stat"]]["wins"] += 1

    stat_summary = {}
    for s, v in sorted(by_stat.items()):
        n = v["bets"]
        stat_summary[s] = {
            "bets":    n,
            "wins":    v["wins"],
            "hitRate": round(v["wins"] / n * 100, 1) if n else None,
            "roi":     round(v["pnl"] / v["wagered"] * 100, 2) if v["wagered"] else None,
            "pnl":     round(v["pnl"], 3),
        }

    return {
        "success":    True,
        "windowDays": window_days,
        "total":      len(bets),
        "settled":    len(settled),
        "pending":    len(pending),
        "wins":       len(wins),
        "losses":     len(losses),
        "pushes":     len(pushes),
        "hitRate":    hit_rate,
        "roi":        roi,
        "totalPnl":   round(total_pnl, 3),
        "byStatBreakdown": stat_summary,
    }


_SUB = {
    "add":    _handle_add,
    "settle": _handle_settle,
    "list":   _handle_list,
    "summary": _handle_summary,
}


def _handle_manual_bet(argv):
    if len(argv) < 3 or argv[2] not in _SUB:
        return {
            "error": "Usage: manual_bet <add|settle|list|summary> ...",
            "subcommands": {
                "add":     "manual_bet add <player> <stat> <over|under> <line> <odds> [--units N] [--date YYYY-MM-DD] [--notes TEXT]",
                "settle":  "manual_bet settle <player> <stat> <actual_value> [--date YYYY-MM-DD]",
                "list":    "manual_bet list [--date YYYY-MM-DD] [--pending] [--stat <stat>]",
                "summary": "manual_bet summary [--window-days N] [--stat <stat>]",
            },
        }
    return _SUB[argv[2]](argv)


_COMMANDS = {"manual_bet": _handle_manual_bet}
