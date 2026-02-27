#!/usr/bin/env python3
"""LLM analysis CLI commands."""

import json

from nba_api.stats.static import players as nba_players_static

from core.nba_llm_engine import llm_full_analysis, llm_injury_signal, llm_matchup_context, llm_line_reasoning
from core.nba_injury_news import fetch_nba_injury_news
from core.nba_data_prep import compute_projection
from core.nba_model_training import compute_ev

from .shared import resolve_player_or_result


def _resolve_player_name(player_arg):
    """Returns (player_id, player_name) or (None, error_dict)."""
    player_id, err = resolve_player_or_result(player_arg)
    if err:
        return None, err
    p = nba_players_static.find_player_by_id(player_id)
    player_name = p.get("full_name") if p else str(player_arg)
    return player_id, player_name


def handle_llm_command(command, argv):
    if command == "llm_analyze":
        # Usage: llm_analyze <player> <team> <opponent> <is_home> <stat> <line> [over_odds] [under_odds]
        if len(argv) < 8:
            return {
                "success": False,
                "error": "Usage: llm_analyze <player> <team> <opponent> <is_home> <stat> <line> [over_odds] [under_odds]",
            }
        player_arg = argv[2]
        team_abbr = str(argv[3]).upper()
        opponent_abbr = str(argv[4]).upper()
        is_home = str(argv[5]) in ("1", "true", "True")
        stat = str(argv[6]).lower()
        try:
            line = float(argv[7])
        except (ValueError, IndexError):
            return {"success": False, "error": "line must be a number"}

        over_odds = int(argv[8]) if len(argv) > 8 else -110
        under_odds = int(argv[9]) if len(argv) > 9 else -110

        player_id, player_name = _resolve_player_name(player_arg)
        if player_id is None:
            return player_name

        # Fetch news signals for injury layer
        news_data = fetch_nba_injury_news(team_abbr, lookback_hours=24)
        news_signals = []
        if news_data.get("success"):
            # Keep player-specific signals and team-level signals that lack playerId.
            for s in (news_data.get("signals") or []):
                sig_pid = int(s.get("playerId") or 0)
                if sig_pid in (0, int(player_id)):
                    news_signals.append(s)

        # Build real projection/EV context so line reasoning has meaningful inputs.
        projection_value = float(line)
        opponent_defense = None
        matchup_history = None
        ev_data = None
        projection_source = "line_fallback"

        proj_res = compute_projection(
            player_id=player_id,
            opponent_abbr=opponent_abbr,
            is_home=is_home,
            is_b2b=False,
            model_variant="full",
        )
        if proj_res.get("success"):
            stat_proj = (proj_res.get("projections") or {}).get(stat)
            opponent_defense = proj_res.get("opponentDefense")
            matchup_history = proj_res.get("matchupHistory")
            if stat_proj:
                projection_value = float(stat_proj.get("projection") or projection_value)
                stdev_val = stat_proj.get("projStdev") or stat_proj.get("stdev")
                ev_data = compute_ev(
                    projection=projection_value,
                    line=line,
                    over_odds=over_odds,
                    under_odds=under_odds,
                    stdev=stdev_val,
                    stat=stat,
                )
                projection_source = "model_projection"

        analysis = llm_full_analysis(
            player_name=player_name,
            team_abbr=team_abbr,
            stat=stat,
            line=line,
            projection=projection_value,
            opponent_abbr=opponent_abbr,
            is_home=is_home,
            ev_data=ev_data,
            opponent_defense=opponent_defense,
            matchup_history=matchup_history,
            news_signals=news_signals,
        )
        analysis["projectionSource"] = projection_source
        if proj_res.get("success") and proj_res.get("gamesPlayed") is not None:
            analysis["gamesPlayed"] = int(proj_res.get("gamesPlayed") or 0)
        elif not proj_res.get("success"):
            analysis["projectionContextError"] = proj_res.get("error")
        return analysis

    if command == "llm_injury":
        # Usage: llm_injury <team> [lookback_hours]
        if len(argv) < 3:
            return {"success": False, "error": "Usage: llm_injury <team> [lookback_hours]"}
        team_abbr = str(argv[2]).upper()
        lookback = int(argv[3]) if len(argv) > 3 else 24
        player_name = str(argv[4]) if len(argv) > 4 else ""

        news_data = fetch_nba_injury_news(team_abbr, lookback_hours=lookback)
        if not news_data.get("success"):
            return news_data

        signals = news_data.get("signals") or []
        return llm_injury_signal(player_name or team_abbr, team_abbr, signals)

    if command == "llm_line":
        # Usage: llm_line <player> <stat> <line> <projection>
        if len(argv) < 6:
            return {"success": False, "error": "Usage: llm_line <player> <stat> <line> <projection>"}
        player_id, player_name = _resolve_player_name(argv[2])
        if player_id is None:
            return player_name
        stat = str(argv[3]).lower()
        try:
            line = float(argv[4])
            projection = float(argv[5])
        except ValueError:
            return {"success": False, "error": "line and projection must be numbers"}
        return llm_line_reasoning(player_name, stat, line, projection)

    return None
