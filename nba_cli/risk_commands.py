#!/usr/bin/env python3
"""Risk metrics CLI command."""

import json
import os

from core.nba_risk_metrics import compute_risk_metrics, filter_bets
from .shared import parse_flags


def _extract_bets(data, model=None):
    """Extract bet records from a backtest artifact.

    Handles three artifact shapes:
    1. ``data["bets"]`` is a list  -> single-model, use directly
    2. ``data["bets"]`` is a dict  -> multi-model ``{model_key: [bets]}``
    3. ``data["reports"][key]["bets"]`` -> fallback per-report extraction

    Returns ``(bets_list, error_dict_or_None)``.
    """
    raw = data.get("bets")

    # Shape 1: top-level list
    if isinstance(raw, list):
        return raw, None

    # Shape 2: top-level dict keyed by model
    if isinstance(raw, dict):
        if model:
            bets = raw.get(model)
            if bets is None:
                available = sorted(raw.keys())
                return None, {
                    "error": (
                        f"Model '{model}' not found in bets. "
                        f"Available: {available}"
                    )
                }
            return bets, None
        # No --model flag
        if len(raw) == 1:
            return list(raw.values())[0], None
        available = sorted(raw.keys())
        return None, {
            "error": (
                f"Multi-model artifact requires --model flag. "
                f"Available: {available}"
            )
        }

    # Shape 3: fallback to reports.*.bets
    reports = data.get("reports", {})
    if model:
        rpt = reports.get(model, {})
        bets = rpt.get("bets")
        if bets:
            return bets, None
        available = sorted(reports.keys())
        return None, {
            "error": (
                f"No bets found for model '{model}' in reports. "
                f"Available: {available}"
            )
        }
    for key in sorted(reports.keys()):
        bets = reports[key].get("bets")
        if bets:
            return bets, None

    return None, {
        "error": (
            "No 'bets' array found in backtest JSON. "
            "Re-run backtest with --emit-all or --emit-bets."
        )
    }


def _handle_risk_metrics(argv):
    """Compute risk metrics from a saved backtest JSON file.

    Usage: risk_metrics <backtest_json_path> [--model full|simple]
                        [--policy-only] [--all-bets] [--real-only]
                        [--stat pts] [--bankroll 100]
    """
    if len(argv) < 3:
        return {
            "error": (
                "Usage: risk_metrics <backtest_json_path> "
                "[--model <full|simple>] "
                "[--policy-only] [--all-bets] [--real-only] [--stat <stat>] "
                "[--bankroll <positive_float>]"
            )
        }

    path = argv[2]
    if not os.path.isfile(path):
        return {"error": f"File not found: {path}"}

    flags = parse_flags(argv, 3, {
        "--model":       ("str", None),
        "--policy-only": ("bool", True),
        "--all-bets":    ("bool", False),
        "--real-only":   ("bool", False),
        "--stat":        ("str", None),
        "--bankroll":    ("float", 100.0),
    })

    if "--policy-only" in argv and "--all-bets" in argv:
        return {"error": "Use either --policy-only or --all-bets, not both"}

    bankroll = flags.get("bankroll", 100.0)
    if bankroll <= 0:
        return {"error": "--bankroll must be > 0"}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    bets, err = _extract_bets(data, model=flags.get("model"))
    if err:
        return err

    # Sort by date as safety measure
    bets.sort(key=lambda b: b.get("date", ""))

    policy_pass_only = not flags.get("all-bets", False)
    filtered = filter_bets(
        bets,
        policy_pass_only=policy_pass_only,
        real_line_only=flags.get("real-only", False),
        stat=flags.get("stat"),
    )

    result = compute_risk_metrics(
        filtered,
        starting_bankroll=bankroll,
    )
    result["filters"] = {
        "policyPassOnly": policy_pass_only,
        "realLineOnly": flags.get("real-only", False),
        "stat": flags.get("stat"),
        "model": flags.get("model"),
        "totalBetsInFile": len(bets),
        "betsAfterFilter": len(filtered),
    }
    result["source"] = path
    return result


_COMMANDS = {
    "risk_metrics": _handle_risk_metrics,
}
