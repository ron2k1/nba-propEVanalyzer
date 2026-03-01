#!/usr/bin/env python3
"""Shared CLI constants and helpers."""

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
