#!/usr/bin/env python3
"""Backtest and minutes-evaluation CLI commands."""

import json
import os
from datetime import datetime, date, timedelta, timezone

from core.nba_backtest import run_backtest, run_minutes_eval


def _handle_backtest(argv):
    if len(argv) < 3:
        return {
            "error": (
                "Usage: backtest <date_from:YYYY-MM-DD> [date_to:YYYY-MM-DD] "
                "[--model full|simple|both] [--save] [--fast] "
                "[--data-source nba|bref|local] [--local] [--bref-dir <path>] "
                "[--local-index <path>] [--odds-source local_history] [--odds-db <path>] "
                "[--real-only] [--clv] [--walk-forward]"
            )
        }
    date_from = argv[2]
    idx = 3
    date_to = None
    if idx < len(argv) and not str(argv[idx]).startswith("--"):
        date_to = argv[idx]
        idx += 1

    model = "both"
    save_results = False
    fast = False
    data_source = "nba"
    bref_dir = None
    odds_source = None
    odds_db = None
    local_index = None
    odds_only = False
    compute_clv = False
    walk_forward = False
    while idx < len(argv):
        token = str(argv[idx]).strip().lower()
        if token == "--model" and idx + 1 < len(argv):
            model = str(argv[idx + 1]).strip().lower()
            idx += 2
            continue
        if token == "--save":
            save_results = True
            idx += 1
            continue
        if token == "--fast":
            fast = True
            idx += 1
            continue
        if token == "--local":
            data_source = "local"
            idx += 1
            continue
        if token == "--data-source" and idx + 1 < len(argv):
            data_source = str(argv[idx + 1]).strip().lower()
            idx += 2
            continue
        if token == "--bref-dir" and idx + 1 < len(argv):
            bref_dir = str(argv[idx + 1]).strip()
            idx += 2
            continue
        if token == "--local-index" and idx + 1 < len(argv):
            local_index = str(argv[idx + 1]).strip()
            idx += 2
            continue
        if token == "--odds-source" and idx + 1 < len(argv):
            odds_source = str(argv[idx + 1]).strip().lower()
            idx += 2
            continue
        if token == "--odds-db" and idx + 1 < len(argv):
            odds_db = str(argv[idx + 1]).strip()
            idx += 2
            continue
        if token == "--real-only":
            odds_only = True
            idx += 1
            continue
        if token == "--clv":
            compute_clv = True
            idx += 1
            continue
        if token == "--walk-forward":
            walk_forward = True
            idx += 1
            continue
        return {
            "error": (
                "Invalid backtest arguments. "
                "Usage: backtest <date_from> [date_to] [--model full|simple|both] "
                "[--save] [--fast] [--data-source nba|bref|local] [--local] "
                "[--bref-dir <path>] [--local-index <path>] "
                "[--odds-source local_history] [--odds-db <path>] [--real-only] [--clv] "
                "[--walk-forward]"
            )
        }
    return run_backtest(date_from=date_from, date_to=date_to, model=model,
                        save_results=save_results, fast=fast,
                        data_source=data_source, bref_dir=bref_dir,
                        odds_source=odds_source, odds_db=odds_db,
                        local_index=local_index, odds_only=odds_only,
                        compute_clv=compute_clv,
                        walk_forward=walk_forward)


def _handle_backtest_60d(argv):
    # Defaults
    window_days = 60
    date_to_str = None
    log_file = None
    odds_db = None

    idx = 2
    while idx < len(argv):
        tok = str(argv[idx]).strip()
        if tok == "--window-days" and idx + 1 < len(argv):
            try:
                window_days = int(argv[idx + 1])
            except ValueError:
                pass
            idx += 2
        elif tok == "--log-file" and idx + 1 < len(argv):
            log_file = str(argv[idx + 1]).strip()
            idx += 2
        elif tok == "--odds-db" and idx + 1 < len(argv):
            odds_db = str(argv[idx + 1]).strip()
            idx += 2
        elif not tok.startswith("-") and date_to_str is None:
            date_to_str = tok
            idx += 1
        else:
            idx += 1

    # Resolve date_to (default: yesterday)
    if date_to_str is None:
        date_to_str = (date.today() - timedelta(days=1)).isoformat()
    try:
        date_to_obj = date.fromisoformat(date_to_str)
    except ValueError:
        return {"success": False, "error": f"Invalid date_to: {date_to_str}. Use YYYY-MM-DD."}

    date_from_obj = date_to_obj - timedelta(days=window_days - 1)
    date_from_str = date_from_obj.isoformat()

    result = run_backtest(
        date_from=date_from_str,
        date_to=date_to_str,
        model="full",
        save_results=True,
        data_source="local",
        odds_source="local_history",
        odds_db=odds_db,
    )

    if not result.get("success", True) or "error" in result:
        return result

    # Extract "full" model report (response["reports"]["full"])
    rpt = (result.get("reports") or {}).get("full", {})
    roi_real = rpt.get("roiReal") or {}
    roi_sim  = rpt.get("roiSimulation") or {}

    log_entry = {
        "runAt":              datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dateFrom":           date_from_str,
        "dateTo":             date_to_str,
        "windowDays":         window_days,
        "model":              "full",
        "sampleCount":        rpt.get("sampleCount"),
        "realLineSamples":    rpt.get("realLineSamples"),
        "missingLineSamples": rpt.get("missingLineSamples"),
        "roiRealBets":        roi_real.get("betsPlaced"),
        "roiRealHitPct":      roi_real.get("hitRatePct"),
        "roiRealPctPerBet":   roi_real.get("roiPctPerBet"),
        "roiSimBets":         roi_sim.get("betsPlaced"),
        "roiSimHitPct":       roi_sim.get("hitRatePct"),
        "roiSimPctPerBet":    roi_sim.get("roiPctPerBet"),
        "oddsSource":         result.get("oddsSource"),
        "savedTo":            result.get("savedTo"),
    }

    # Append to log file
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if log_file is None:
        log_file = os.path.join(_root, "data", "backtest_60d_log.jsonl")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as _lf:
        _lf.write(json.dumps(log_entry) + "\n")

    return {
        "success":   True,
        "logEntry":  log_entry,
        "logFile":   log_file,
        "backtest":  {
            "dateFrom":        date_from_str,
            "dateTo":          date_to_str,
            "windowDays":      window_days,
            "sampleCount":     rpt.get("sampleCount"),
            "realLineSamples": rpt.get("realLineSamples"),
            "roiReal":         roi_real,
            "roiSimulation":   roi_sim,
        },
    }


def _handle_minutes_eval(argv):
    if len(argv) < 3:
        return {
            "error": (
                "Usage: minutes_eval <date_from:YYYY-MM-DD> [date_to:YYYY-MM-DD] "
                "[--local] [--data-source nba|bref|local] [--local-index <path>]"
            )
        }

    date_from   = argv[2]
    date_to     = None
    data_source = "nba"
    local_index = None

    idx = 3
    if idx < len(argv) and not str(argv[idx]).startswith("-"):
        date_to = argv[idx]
        idx += 1

    while idx < len(argv):
        tok = str(argv[idx]).strip().lower()
        if tok == "--local":
            data_source = "local"
        elif tok == "--data-source" and idx + 1 < len(argv):
            data_source = str(argv[idx + 1]).strip().lower()
            idx += 1
        elif tok == "--local-index" and idx + 1 < len(argv):
            local_index = str(argv[idx + 1]).strip()
            idx += 1
        idx += 1

    return run_minutes_eval(
        date_from=date_from,
        date_to=date_to,
        data_source=data_source,
        local_index=local_index,
    )


_COMMANDS = {
    "backtest":     _handle_backtest,
    "backtest_60d": _handle_backtest_60d,
    "minutes_eval": _handle_minutes_eval,
}


def handle_backtest_command(command, argv):  # shim — router no longer calls this
    h = _COMMANDS.get(command)
    return h(argv) if h else None
