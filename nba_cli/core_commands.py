#!/usr/bin/env python3
"""Core data and odds CLI commands."""

from nba_data_collection import (
    get_active_players_for_teams,
    get_all_players,
    get_all_teams,
    get_nba_live_odds,
    get_nba_sportsbook_odds,
    get_player_game_log,
    get_player_position,
    get_player_splits,
    get_position_vs_team,
    get_team_defensive_ratings,
    get_team_roster_status,
    get_todays_games,
    search_players_by_name,
)
from nba_data_prep import compute_usage_adjustment
from nba_injury_news import compute_usage_adjustment_with_news, fetch_nba_injury_news

from .shared import DEFAULT_ODDS_MARKETS, resolve_player_or_result


def handle_core_command(command, argv):
    if command == "games":
        return get_todays_games()

    if command == "teams":
        return get_all_teams()

    if command == "players":
        return get_all_players()

    if command == "player_log":
        if len(argv) < 3:
            return {"error": "player_id_or_name required"}
        player_id, err = resolve_player_or_result(argv[2])
        if err:
            return err
        last_n = int(argv[3]) if len(argv) > 3 else 25
        result = get_player_game_log(player_id, last_n=last_n)
        if result.get("success"):
            result["resolvedPlayerId"] = player_id
        return result

    if command == "player_splits":
        if len(argv) < 3:
            return {"error": "player_id_or_name required"}
        player_id, err = resolve_player_or_result(argv[2])
        if err:
            return err
        result = get_player_splits(player_id)
        if result.get("success"):
            result["resolvedPlayerId"] = player_id
        return result

    if command == "defense":
        return get_team_defensive_ratings()

    if command == "team_players":
        if len(argv) < 3:
            return {"error": "team_ids required (comma-separated)"}
        return get_active_players_for_teams(argv[2])

    if command == "player_position":
        if len(argv) < 3:
            return {"error": "player_id_or_name required"}
        player_id, err = resolve_player_or_result(argv[2])
        if err:
            return err
        return get_player_position(player_id)

    if command == "position_vs_team":
        if len(argv) < 3:
            return {"error": "team_id (numeric) required"}
        return get_position_vs_team(int(argv[2]))

    if command == "roster_status":
        if len(argv) < 3:
            return {"error": "Usage: roster_status <team_abbr>  e.g. roster_status LAL"}
        return get_team_roster_status(argv[2])

    if command == "usage_adjust":
        if len(argv) < 4:
            return {"error": "Usage: usage_adjust <player_id_or_name> <team_abbr>  e.g. usage_adjust 2544 LAL"}
        player_id, err = resolve_player_or_result(argv[2])
        if err:
            return err
        result = compute_usage_adjustment(player_id, argv[3])
        if result.get("success"):
            result["resolvedPlayerId"] = player_id
        return result

    if command == "usage_adjust_news":
        if len(argv) < 4:
            return {
                "error": (
                    "Usage: usage_adjust_news <player_id_or_name> <team_abbr> [lookback_hours] "
                    "e.g. usage_adjust_news 2544 LAL 24"
                )
            }
        player_id, err = resolve_player_or_result(argv[2])
        if err:
            return err
        lookback_hours = int(argv[4]) if len(argv) > 4 else 24
        result = compute_usage_adjustment_with_news(player_id, argv[3], lookback_hours=lookback_hours)
        if result.get("success"):
            result["resolvedPlayerId"] = player_id
        return result

    if command == "injury_news":
        if len(argv) < 3:
            return {"error": "Usage: injury_news <team_abbr> [lookback_hours]"}
        lookback_hours = int(argv[3]) if len(argv) > 3 else 24
        return fetch_nba_injury_news(argv[2], lookback_hours=lookback_hours)

    if command == "player_lookup":
        if len(argv) < 3:
            return {"error": "Usage: player_lookup <name_query> [limit]"}
        name_query = argv[2]
        limit = int(argv[3]) if len(argv) > 3 else 20
        return search_players_by_name(name_query, limit=limit)

    if command == "odds":
        regions = argv[2] if len(argv) > 2 else "us"
        markets = argv[3] if len(argv) > 3 else DEFAULT_ODDS_MARKETS
        bookmakers = argv[4] if len(argv) > 4 else None
        sport = argv[5] if len(argv) > 5 else "basketball_nba"
        return get_nba_sportsbook_odds(
            regions=regions,
            markets=markets,
            bookmakers=bookmakers,
            sport=sport,
        )

    if command == "odds_live":
        regions = argv[2] if len(argv) > 2 else "us"
        markets = argv[3] if len(argv) > 3 else DEFAULT_ODDS_MARKETS
        bookmakers = argv[4] if len(argv) > 4 else None
        sport = argv[5] if len(argv) > 5 else "basketball_nba"
        max_events = int(argv[6]) if len(argv) > 6 else 8
        return get_nba_live_odds(
            regions=regions,
            markets=markets,
            bookmakers=bookmakers,
            sport=sport,
            max_events=max_events,
        )

    return None
