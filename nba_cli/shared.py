#!/usr/bin/env python3
"""Shared CLI constants and helpers."""

from datetime import datetime

from core.nba_data_collection import resolve_player_identifier

DEFAULT_ODDS_MARKETS = "h2h,spreads,totals"
AVAILABLE_COMMANDS = (
    "games, teams, players, player_log, player_splits, defense, team_players, "
    "player_position, position_vs_team, projection, prop_ev, prop_ev_ml, auto_sweep, "
    "roster_status, usage_adjust, usage_adjust_news, injury_news, "
    "llm_analyze, llm_injury, llm_line, "
    "parlay_ev, odds, odds_live, "
    "player_lookup, settle_yesterday, best_today, "
    "results_yesterday, export_training_rows, record_closing, train_model, starter_accuracy, "
    "train_projection_ml, train_projection_ml_per_stat, train_quantile_projection, promote_projection_ml, backtest, live_projection"
)
VALID_LIVE_PROJECTION_STATS = {"pts", "reb", "ast", "fg3m", "pra", "stl", "blk", "tov"}


def no_command_payload():
    return {"error": f"No command specified. Available: {AVAILABLE_COMMANDS}"}


def resolve_player_or_result(identifier):
    resolved = resolve_player_identifier(identifier)
    if resolved.get("success"):
        return int(resolved["playerId"]), None

    error_payload = {"error": resolved.get("error", "Invalid player identifier")}
    if resolved.get("ambiguous"):
        error_payload["candidates"] = resolved.get("candidates", [])
    return None, error_payload


def _looks_like_date(s: str) -> bool:
    try:
        datetime.strptime(str(s or "")[:10], "%Y-%m-%d")
        return True
    except ValueError:
        return False


def parse_csv(value: str) -> list:
    return [s.strip() for s in str(value or "").split(",") if s.strip()]


def safe_int(value, default=None):
    try: return int(value)
    except (ValueError, TypeError): return default


def safe_float(value, default=None):
    try: return float(value)
    except (ValueError, TypeError): return default


def usage_error(msg: str) -> dict:
    return {"error": f"Usage: {msg}"}


def parse_flags(argv: list, start: int, spec: dict) -> dict:
    """
    spec = {"--model": ("str", "full"), "--save": ("bool", False), "--limit": ("int", 10)}
    Returns {key_without_dashes: parsed_value}.
    """
    result = {k.lstrip("-"): v for k, (_, v) in spec.items()}
    idx = start
    while idx < len(argv):
        tok = str(argv[idx]).strip()
        if tok in spec:
            typ, _ = spec[tok]
            key = tok.lstrip("-")
            if typ == "bool":
                result[key] = True; idx += 1
            elif idx + 1 < len(argv):
                raw = str(argv[idx + 1]).strip()
                result[key] = (int(raw) if typ == "int" else
                               float(raw) if typ == "float" else raw)
                idx += 2
            else:
                idx += 1
        else:
            idx += 1
    return result
