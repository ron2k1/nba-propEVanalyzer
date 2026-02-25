#!/usr/bin/env python3
"""
NBA Pipeline CLI dispatcher.

Core logic is split across:
  - nba_data_collection.py
  - nba_data_prep.py
  - nba_model_training.py
"""

import json
import os
import sys
import traceback

from nba_data_collection import (
    get_todays_games,
    get_all_teams,
    get_all_players,
    get_player_game_log,
    get_player_splits,
    get_team_defensive_ratings,
    get_active_players_for_teams,
    get_player_position,
    get_position_vs_team,
    get_team_roster_status,
    search_players_by_name,
    resolve_player_identifier,
    get_nba_sportsbook_odds,
    get_nba_live_odds,
    safe_round,
)
from nba_data_prep import compute_projection, compute_usage_adjustment
from nba_model_training import (
    compute_ev,
    compute_prop_ev,
    compute_prop_ev_with_ml,
    compute_auto_line_sweep,
    compute_parlay_ev,
    train_projection_ml_from_file,
    promote_projection_ml_model,
    train_ridge_calibrator_from_file,
)

DEFAULT_ODDS_MARKETS = "h2h,spreads,totals"
from nba_bet_tracking import (
    log_prop_ev_entry,
    settle_yesterday,
    settle_entries_for_date,
    best_today,
    best_plays_for_date,
    results_yesterday,
    results_for_date,
    export_training_rows,
    record_closing_values,
)
from nba_injury_news import (
    fetch_nba_injury_news,
    compute_usage_adjustment_with_news,
)
from nba_backtest import run_backtest

def _resolve_player_or_result(identifier):
    resolved = resolve_player_identifier(identifier)
    if resolved.get("success"):
        return int(resolved["playerId"]), None

    error_payload = {"error": resolved.get("error", "Invalid player identifier")}
    if resolved.get("ambiguous"):
        error_payload["candidates"] = resolved.get("candidates", [])
    return None, error_payload


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({
            "error": (
                "No command specified. Available: games, teams, players, "
                "player_log, player_splits, defense, team_players, "
                "player_position, position_vs_team, projection, prop_ev, prop_ev_ml, auto_sweep, "
                "roster_status, usage_adjust, usage_adjust_news, injury_news, "
                "parlay_ev, odds, odds_live, "
                "player_lookup, settle_yesterday, best_today, "
                "results_yesterday, export_training_rows, record_closing, train_model, "
                "train_projection_ml, promote_projection_ml, backtest"
            )
        }))
        sys.exit(1)

    command = sys.argv[1]

    try:
        if command == "games":
            result = get_todays_games()

        elif command == "teams":
            result = get_all_teams()

        elif command == "players":
            result = get_all_players()

        elif command == "player_log":
            if len(sys.argv) < 3:
                result = {"error": "player_id_or_name required"}
            else:
                player_id, err = _resolve_player_or_result(sys.argv[2])
                if err:
                    result = err
                else:
                    last_n = int(sys.argv[3]) if len(sys.argv) > 3 else 25
                    result = get_player_game_log(player_id, last_n=last_n)
                    if result.get("success"):
                        result["resolvedPlayerId"] = player_id

        elif command == "player_splits":
            if len(sys.argv) < 3:
                result = {"error": "player_id_or_name required"}
            else:
                player_id, err = _resolve_player_or_result(sys.argv[2])
                if err:
                    result = err
                else:
                    result = get_player_splits(player_id)
                    if result.get("success"):
                        result["resolvedPlayerId"] = player_id

        elif command == "defense":
            result = get_team_defensive_ratings()

        elif command == "team_players":
            if len(sys.argv) < 3:
                result = {"error": "team_ids required (comma-separated)"}
            else:
                result = get_active_players_for_teams(sys.argv[2])

        elif command == "player_position":
            if len(sys.argv) < 3:
                result = {"error": "player_id_or_name required"}
            else:
                player_id, err = _resolve_player_or_result(sys.argv[2])
                if err:
                    result = err
                else:
                    result = get_player_position(player_id)

        elif command == "position_vs_team":
            if len(sys.argv) < 3:
                result = {"error": "team_id (numeric) required"}
            else:
                result = get_position_vs_team(int(sys.argv[2]))

        elif command == "projection":
            # projection <player_id_or_name> <opponent_abbr> <is_home:0|1> [is_b2b:0|1]
            if len(sys.argv) < 5:
                result = {"error": "Usage: projection <player_id_or_name> <opponent_abbr> <is_home:0|1> [is_b2b:0|1]"}
            else:
                player_id, err = _resolve_player_or_result(sys.argv[2])
                if err:
                    result = err
                else:
                    opponent = sys.argv[3]
                    is_home = sys.argv[4] == "1"
                    is_b2b = (sys.argv[5] == "1") if len(sys.argv) > 5 else False
                    result = compute_projection(player_id, opponent, is_home, is_b2b)
                    if result.get("success"):
                        result["resolvedPlayerId"] = player_id

        elif command == "prop_ev":
            # prop_ev <player_id_or_name> <opponent_abbr> <is_home:0|1> <stat>
            #         <line> <over_odds> <under_odds> [is_b2b:0|1] [player_team_abbr]
            if len(sys.argv) < 9:
                result = {
                    "error": (
                        "Usage: prop_ev <player_id_or_name> <opponent_abbr> <is_home:0|1> "
                        "<stat> <line> <over_odds> <under_odds> [is_b2b:0|1] [player_team_abbr]"
                    )
                }
            else:
                player_id, err = _resolve_player_or_result(sys.argv[2])
                if err:
                    result = err
                else:
                    opponent = sys.argv[3]
                    is_home = sys.argv[4] == "1"
                    stat = sys.argv[5]
                    line = float(sys.argv[6])
                    over_odds = int(sys.argv[7])
                    under_odds = int(sys.argv[8])
                    is_b2b = (sys.argv[9] == "1") if len(sys.argv) > 9 else False
                    player_team_abbr = sys.argv[10] if len(sys.argv) > 10 else None

                    result = compute_prop_ev(
                        player_id=player_id,
                        opponent_abbr=opponent,
                        is_home=is_home,
                        stat=stat,
                        line=line,
                        over_odds=over_odds,
                        under_odds=under_odds,
                        is_b2b=is_b2b,
                        player_team_abbr=player_team_abbr,
                    )

                    # Optional usage-layer adjustment on top of projection
                    if result.get("success") and player_team_abbr:
                        usage_data = compute_usage_adjustment(player_id, player_team_abbr)
                        if usage_data.get("success") and usage_data.get("absentTeammates"):
                            proj = dict(result.get("projection") or {})
                            stat_mult = (usage_data.get("statMultipliers") or {}).get(stat, 1.0)
                            base_proj = proj.get("projection")
                            if base_proj is not None:
                                proj["projectionPreUsage"] = base_proj
                                proj["projection"] = safe_round(base_proj * stat_mult, 1)
                                proj["usageMultiplier"] = stat_mult
                                stdev_val = proj.get("projStdev") or proj.get("stdev") or 0
                                ev_over_odds = int(result.get("bestOverOdds") or over_odds)
                                ev_under_odds = int(result.get("bestUnderOdds") or under_odds)
                                ev_data = compute_ev(
                                    proj["projection"],
                                    line,
                                    ev_over_odds,
                                    ev_under_odds,
                                    stdev_val,
                                )
                                result["projection"] = proj
                                result["ev"] = ev_data
                                result["usageAdjustment"] = usage_data
                        else:
                            result["usageAdjustment"] = None
                    if result.get("success"):
                        result["resolvedPlayerId"] = player_id
                        journal_res = log_prop_ev_entry(
                            result,
                            player_id=player_id,
                            player_identifier=sys.argv[2],
                            player_team_abbr=player_team_abbr,
                            opponent_abbr=opponent,
                            is_home=is_home,
                            stat=stat,
                            line=line,
                            over_odds=over_odds,
                            under_odds=under_odds,
                            is_b2b=is_b2b,
                            source="cli",
                        )
                        if journal_res.get("success"):
                            result["journalEntryId"] = journal_res.get("entryId")
                        else:
                            result["journalError"] = journal_res.get("error")

        elif command == "prop_ev_ml":
            # prop_ev_ml <player_id_or_name> <opponent_abbr> <is_home:0|1> <stat>
            #            <line> <over_odds> <under_odds> [is_b2b:0|1] [model_path]
            if len(sys.argv) < 9:
                result = {
                    "error": (
                        "Usage: prop_ev_ml <player_id_or_name> <opponent_abbr> <is_home:0|1> "
                        "<stat> <line> <over_odds> <under_odds> [is_b2b:0|1] [model_path]"
                    )
                }
            else:
                player_id, err = _resolve_player_or_result(sys.argv[2])
                if err:
                    result = err
                else:
                    opponent = sys.argv[3]
                    is_home = sys.argv[4] == "1"
                    stat = sys.argv[5]
                    line = float(sys.argv[6])
                    over_odds = int(sys.argv[7])
                    under_odds = int(sys.argv[8])
                    is_b2b = (sys.argv[9] == "1") if len(sys.argv) > 9 else False
                    model_path = sys.argv[10] if len(sys.argv) > 10 else None
                    kwargs = {}
                    if model_path:
                        kwargs["model_path"] = model_path
                    result = compute_prop_ev_with_ml(
                        player_id=player_id,
                        opponent_abbr=opponent,
                        is_home=is_home,
                        stat=stat,
                        line=line,
                        over_odds=over_odds,
                        under_odds=under_odds,
                        is_b2b=is_b2b,
                        **kwargs,
                    )
                    if result.get("success"):
                        result["resolvedPlayerId"] = player_id

        elif command == "auto_sweep":
            # auto_sweep <player_id_or_name> <player_team_abbr> <opponent_abbr> <is_home:0|1> <stat>
            #            [is_b2b:0|1] [regions] [bookmakers_csv] [sport] [top_n]
            if len(sys.argv) < 7:
                result = {
                    "error": (
                        "Usage: auto_sweep <player_id_or_name> <player_team_abbr> "
                        "<opponent_abbr> <is_home:0|1> <stat> "
                        "[is_b2b:0|1] [regions] [bookmakers_csv] [sport] [top_n]"
                    )
                }
            else:
                player_id, err = _resolve_player_or_result(sys.argv[2])
                if err:
                    result = err
                else:
                    player_team_abbr = str(sys.argv[3]).upper()
                    opponent = str(sys.argv[4]).upper()
                    is_home = sys.argv[5] == "1"
                    stat = sys.argv[6]
                    is_b2b = (sys.argv[7] == "1") if len(sys.argv) > 7 else False
                    regions = sys.argv[8] if len(sys.argv) > 8 else "us"
                    bookmakers = sys.argv[9] if len(sys.argv) > 9 else None
                    sport = sys.argv[10] if len(sys.argv) > 10 else "basketball_nba"
                    top_n = int(sys.argv[11]) if len(sys.argv) > 11 else 15

                    result = compute_auto_line_sweep(
                        player_id=player_id,
                        player_team_abbr=player_team_abbr,
                        opponent_abbr=opponent,
                        is_home=is_home,
                        stat=stat,
                        is_b2b=is_b2b,
                        regions=regions,
                        bookmakers=bookmakers,
                        sport=sport,
                        top_n=top_n,
                    )
                    if result.get("success"):
                        result["resolvedPlayerId"] = player_id
                        best = result.get("bestRecommendation") or {}
                        best_ev = best.get("ev")
                        if best_ev:
                            journal_like = {
                                "success": True,
                                "projection": result.get("projection"),
                                "ev": best_ev,
                            }
                            journal_res = log_prop_ev_entry(
                                journal_like,
                                player_id=player_id,
                                player_identifier=sys.argv[2],
                                player_team_abbr=player_team_abbr,
                                opponent_abbr=opponent,
                                is_home=is_home,
                                stat=stat,
                                line=best.get("line"),
                                over_odds=best.get("overOdds"),
                                under_odds=best.get("underOdds"),
                                is_b2b=is_b2b,
                                source="auto_sweep",
                            )
                            if journal_res.get("success"):
                                result["journalEntryId"] = journal_res.get("entryId")
                            else:
                                result["journalError"] = journal_res.get("error")

        elif command == "train_projection_ml":
            # train_projection_ml <data_path> [target_key] [feature_keys_csv|auto]
            #                    [holdout_frac] [min_holdout] [model_type] [date_key] [output_model_path]
            if len(sys.argv) < 3:
                result = {
                    "error": (
                        "Usage: train_projection_ml <data_path> [target_key] [feature_keys_csv|auto] "
                        "[holdout_frac] [min_holdout] [model_type] [date_key] [output_model_path]"
                    )
                }
            else:
                data_path = sys.argv[2]
                target_key = sys.argv[3] if len(sys.argv) > 3 else "actual"
                feature_arg = sys.argv[4] if len(sys.argv) > 4 else "auto"
                holdout_frac = float(sys.argv[5]) if len(sys.argv) > 5 else 0.2
                min_holdout = int(sys.argv[6]) if len(sys.argv) > 6 else 50
                model_type = sys.argv[7] if len(sys.argv) > 7 else "gradient_boosting"
                date_key = sys.argv[8] if len(sys.argv) > 8 else "pickDate"
                output_model_path = sys.argv[9] if len(sys.argv) > 9 else None

                feature_keys = None if feature_arg.lower() == "auto" else [
                    k.strip() for k in feature_arg.split(",") if k.strip()
                ]

                result = train_projection_ml_from_file(
                    data_path=data_path,
                    target_key=target_key,
                    feature_keys=feature_keys,
                    holdout_frac=holdout_frac,
                    min_holdout=min_holdout,
                    model_type=model_type,
                    date_key=date_key,
                    output_model_path=output_model_path,
                )

        elif command == "promote_projection_ml":
            # promote_projection_ml <candidate_model_path> [production_model_path]
            #                      [min_rmse_improve_pct] [min_mae_improve_pct] [force:0|1]
            if len(sys.argv) < 3:
                result = {
                    "error": (
                        "Usage: promote_projection_ml <candidate_model_path> [production_model_path] "
                        "[min_rmse_improve_pct] [min_mae_improve_pct] [force:0|1]"
                    )
                }
            else:
                candidate_model_path = sys.argv[2]
                production_model_path = sys.argv[3] if len(sys.argv) > 3 else None
                min_rmse_improve_pct = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
                min_mae_improve_pct = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
                force = (sys.argv[6] == "1") if len(sys.argv) > 6 else False

                kwargs = {}
                if production_model_path:
                    kwargs["production_model_path"] = production_model_path

                result = promote_projection_ml_model(
                    candidate_model_path=candidate_model_path,
                    min_rmse_improve_pct=min_rmse_improve_pct,
                    min_mae_improve_pct=min_mae_improve_pct,
                    force=force,
                    **kwargs,
                )

        elif command == "roster_status":
            # roster_status <team_abbr>
            if len(sys.argv) < 3:
                result = {"error": "Usage: roster_status <team_abbr>  e.g. roster_status LAL"}
            else:
                result = get_team_roster_status(sys.argv[2])

        elif command == "usage_adjust":
            # usage_adjust <player_id_or_name> <team_abbr>
            if len(sys.argv) < 4:
                result = {"error": "Usage: usage_adjust <player_id_or_name> <team_abbr>  e.g. usage_adjust 2544 LAL"}
            else:
                player_id, err = _resolve_player_or_result(sys.argv[2])
                if err:
                    result = err
                else:
                    result = compute_usage_adjustment(player_id, sys.argv[3])
                    if result.get("success"):
                        result["resolvedPlayerId"] = player_id

        elif command == "usage_adjust_news":
            # usage_adjust_news <player_id_or_name> <team_abbr> [lookback_hours]
            if len(sys.argv) < 4:
                result = {
                    "error": (
                        "Usage: usage_adjust_news <player_id_or_name> <team_abbr> [lookback_hours] "
                        "e.g. usage_adjust_news 2544 LAL 24"
                    )
                }
            else:
                player_id, err = _resolve_player_or_result(sys.argv[2])
                if err:
                    result = err
                else:
                    lookback_hours = int(sys.argv[4]) if len(sys.argv) > 4 else 24
                    result = compute_usage_adjustment_with_news(
                        player_id, sys.argv[3], lookback_hours=lookback_hours
                    )
                    if result.get("success"):
                        result["resolvedPlayerId"] = player_id

        elif command == "injury_news":
            # injury_news <team_abbr> [lookback_hours]
            if len(sys.argv) < 3:
                result = {"error": "Usage: injury_news <team_abbr> [lookback_hours]"}
            else:
                lookback_hours = int(sys.argv[3]) if len(sys.argv) > 3 else 24
                result = fetch_nba_injury_news(sys.argv[2], lookback_hours=lookback_hours)

        elif command == "parlay_ev":
            # parlay_ev '<json_array_of_legs>'
            if len(sys.argv) < 3:
                result = {
                    "error": (
                        "Usage: parlay_ev '<json_legs>' where json_legs is a JSON array. "
                        "Each leg needs: playerId, playerTeam, stat, line, side, "
                        "probOver, overOdds, underOdds. 2-3 legs supported."
                    )
                }
            else:
                try:
                    legs = json.loads(sys.argv[2])
                    if not isinstance(legs, list):
                        result = {"error": "parlay_ev argument must be a JSON array of legs"}
                    else:
                        result = compute_parlay_ev(legs)
                except json.JSONDecodeError as je:
                    result = {"error": f"Invalid JSON for parlay legs: {je}"}

        elif command == "player_lookup":
            # player_lookup <name_query> [limit]
            if len(sys.argv) < 3:
                result = {"error": "Usage: player_lookup <name_query> [limit]"}
            else:
                name_query = sys.argv[2]
                limit = int(sys.argv[3]) if len(sys.argv) > 3 else 20
                result = search_players_by_name(name_query, limit=limit)

        elif command == "settle_yesterday":
            # settle_yesterday [date_yyyy-mm-dd]
            if len(sys.argv) > 2:
                result = settle_entries_for_date(sys.argv[2])
            else:
                result = settle_yesterday()

        elif command == "best_today":
            # best_today [limit] [date_yyyy-mm-dd]
            limit = int(sys.argv[2]) if len(sys.argv) > 2 else 15
            if len(sys.argv) > 3:
                result = best_plays_for_date(sys.argv[3], limit=limit)
            else:
                result = best_today(limit=limit)

        elif command == "results_yesterday":
            # results_yesterday [limit] [date_yyyy-mm-dd]
            limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50
            if len(sys.argv) > 3:
                result = results_for_date(sys.argv[3], limit=limit)
            else:
                result = results_yesterday(limit=limit)

        elif command == "export_training_rows":
            # export_training_rows <output_path> [format:csv|jsonl] [date_from] [date_to]
            if len(sys.argv) < 3:
                result = {
                    "error": (
                        "Usage: export_training_rows <output_path> "
                        "[format:csv|jsonl] [date_from:YYYY-MM-DD] [date_to:YYYY-MM-DD]"
                    )
                }
            else:
                output_path = sys.argv[2]
                fmt = sys.argv[3] if len(sys.argv) > 3 else None
                date_from = sys.argv[4] if len(sys.argv) > 4 else None
                date_to = sys.argv[5] if len(sys.argv) > 5 else None
                result = export_training_rows(
                    output_path=output_path,
                    fmt=fmt,
                    date_from=date_from,
                    date_to=date_to,
                )

        elif command == "record_closing":
            # record_closing <date_yyyy-mm-dd> '<json_updates>'
            if len(sys.argv) < 4:
                result = {
                    "error": (
                        "Usage: record_closing <date:YYYY-MM-DD> '<json_updates>' "
                        "where each update includes entryId + closingLine/closingOdds"
                    )
                }
            else:
                date_str = sys.argv[2]
                try:
                    updates = json.loads(sys.argv[3])
                except json.JSONDecodeError as je:
                    result = {"error": f"Invalid JSON for closing updates: {je}"}
                else:
                    result = record_closing_values(date_str, updates)

        elif command == "backtest":
            # backtest <date_from> [date_to] [--model full|simple|both]
            if len(sys.argv) < 3:
                result = {
                    "error": (
                        "Usage: backtest <date_from:YYYY-MM-DD> [date_to:YYYY-MM-DD] "
                        "[--model full|simple|both]"
                    )
                }
            else:
                date_from = sys.argv[2]
                idx = 3
                date_to = None
                if idx < len(sys.argv) and not str(sys.argv[idx]).startswith("--"):
                    date_to = sys.argv[idx]
                    idx += 1

                model = "both"
                while idx < len(sys.argv):
                    token = str(sys.argv[idx]).strip().lower()
                    if token == "--model" and idx + 1 < len(sys.argv):
                        model = str(sys.argv[idx + 1]).strip().lower()
                        idx += 2
                        continue
                    result = {
                        "error": (
                            "Invalid backtest arguments. "
                            "Usage: backtest <date_from> [date_to] [--model full|simple|both]"
                        )
                    }
                    break
                else:
                    result = run_backtest(date_from=date_from, date_to=date_to, model=model)

        elif command == "odds":
            # odds [regions] [markets] [bookmakers_csv] [sport]
            regions = sys.argv[2] if len(sys.argv) > 2 else "us"
            markets = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_ODDS_MARKETS
            bookmakers = sys.argv[4] if len(sys.argv) > 4 else None
            sport = sys.argv[5] if len(sys.argv) > 5 else "basketball_nba"
            result = get_nba_sportsbook_odds(
                regions=regions,
                markets=markets,
                bookmakers=bookmakers,
                sport=sport,
            )

        elif command == "odds_live":
            # odds_live [regions] [markets] [bookmakers_csv] [sport] [max_events]
            regions = sys.argv[2] if len(sys.argv) > 2 else "us"
            markets = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_ODDS_MARKETS
            bookmakers = sys.argv[4] if len(sys.argv) > 4 else None
            sport = sys.argv[5] if len(sys.argv) > 5 else "basketball_nba"
            max_events = int(sys.argv[6]) if len(sys.argv) > 6 else 8
            result = get_nba_live_odds(
                regions=regions,
                markets=markets,
                bookmakers=bookmakers,
                sport=sport,
                max_events=max_events,
            )

        elif command == "train_model":
            # train_model <data_path> [target_key] [feature_keys_csv|auto] [ridge_alpha] [output_model_path]
            if len(sys.argv) < 3:
                result = {
                    "error": (
                        "Usage: train_model <data_path> [target_key] [feature_keys_csv|auto] "
                        "[ridge_alpha] [output_model_path]"
                    )
                }
            else:
                data_path = sys.argv[2]
                target_key = sys.argv[3] if len(sys.argv) > 3 else "actual"
                features_arg = sys.argv[4] if len(sys.argv) > 4 else "auto"
                ridge_alpha = float(sys.argv[5]) if len(sys.argv) > 5 else 0.5

                # Default model output next to the source file.
                if len(sys.argv) > 6:
                    output_model_path = sys.argv[6]
                else:
                    base = os.path.splitext(data_path)[0]
                    output_model_path = base + "_ridge_model.json"

                if features_arg.lower() == "auto":
                    feature_keys = None
                else:
                    feature_keys = [k.strip() for k in features_arg.split(",") if k.strip()]

                result = train_ridge_calibrator_from_file(
                    data_path=data_path,
                    target_key=target_key,
                    feature_keys=feature_keys,
                    ridge_alpha=ridge_alpha,
                    output_model_path=output_model_path,
                )

                # Keep CLI output compact.
                if result.get("success"):
                    model = result.get("model") or {}
                    result = {
                        "success": True,
                        "savedPath": result.get("savedPath"),
                        "trainingRows": result.get("trainingRows"),
                        "featureCount": result.get("featureCount"),
                        "metrics": model.get("metrics"),
                        "targetKey": model.get("targetKey"),
                        "featureKeys": model.get("featureKeys"),
                    }

        else:
            result = {"error": f"Unknown command: {command}"}

        print(json.dumps(result, default=str))

    except Exception as e:
        print(json.dumps({"error": str(e), "traceback": traceback.format_exc()}))
        sys.exit(1)
