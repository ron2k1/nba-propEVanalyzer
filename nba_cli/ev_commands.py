#!/usr/bin/env python3
"""EV CLI commands: prop_ev, prop_ev_ml, auto_sweep, parlay_ev."""

import json

from nba_api.stats.static import players as nba_players_static

from core.nba_bet_tracking import log_prop_ev_entry
from core.nba_decision_journal import DecisionJournal, _qualifies
from core.nba_data_collection import safe_round
from core.nba_data_prep import compute_usage_adjustment
from core.nba_injury_news import fetch_nba_injury_news
from core.nba_llm_engine import llm_full_analysis
from core.nba_model_training import (
    compute_auto_line_sweep,
    compute_ev,
    compute_parlay_ev,
    compute_prop_ev,
    compute_prop_ev_with_ml,
)

from .shared import resolve_player_or_result


def _build_reference_probs(result):
    ref_book = result.get("referenceBook") or {}
    ref_over = ref_book.get("noVigOver")
    ref_under = ref_book.get("noVigUnder")
    if ref_over is None or ref_under is None:
        return None
    try:
        o = float(ref_over)
        u = float(ref_under)
        total = o + u
        if total > 0:
            return {"over": o / total, "under": u / total, "push": 0.0}
    except (TypeError, ValueError):
        return None
    return None


def _apply_usage_adjustment(result, player_id, stat, line, over_odds, under_odds, player_team_abbr):
    if not result.get("success") or not player_team_abbr:
        return result

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
            reference_probs = _build_reference_probs(result)
            ev_data = compute_ev(
                proj["projection"],
                line,
                ev_over_odds,
                ev_under_odds,
                stdev_val,
                stat=stat,
                reference_probs=reference_probs,
            )
            result["projection"] = proj
            result["ev"] = ev_data
            result["usageAdjustment"] = usage_data
    else:
        result["usageAdjustment"] = None
    return result


def _handle_prop_ev(argv):
    if len(argv) < 9:
        return {
            "error": (
                "Usage: prop_ev <player_id_or_name> <opponent_abbr> <is_home:0|1> "
                "<stat> <line> <over_odds> <under_odds> [is_b2b:0|1] [player_team_abbr] [reference_book]"
            )
        }

    player_id, err = resolve_player_or_result(argv[2])
    if err:
        return err

    opponent = argv[3]
    is_home = argv[4] == "1"
    stat = argv[5]
    line = float(argv[6])
    over_odds = int(argv[7])
    under_odds = int(argv[8])
    is_b2b = (argv[9] == "1") if len(argv) > 9 else False
    player_team_abbr = argv[10] if len(argv) > 10 else None
    reference_book = argv[11] if len(argv) > 11 else None
    player_name = str(argv[2])

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
        reference_book=reference_book,
    )

    result = _apply_usage_adjustment(
        result=result,
        player_id=player_id,
        stat=stat,
        line=line,
        over_odds=over_odds,
        under_odds=under_odds,
        player_team_abbr=player_team_abbr,
    )

    if result.get("success"):
        try:
            p = nba_players_static.find_player_by_id(player_id)
            player_name = p.get("full_name") if p else str(argv[2])
            news_signals = []
            if player_team_abbr:
                news_data = fetch_nba_injury_news(player_team_abbr, lookback_hours=24)
                if news_data.get("success"):
                    news_signals = news_data.get("signals") or []
            proj_val = (result.get("projection") or {}).get("projection")
            result["llmAnalysis"] = llm_full_analysis(
                player_name=player_name,
                team_abbr=player_team_abbr or "",
                stat=stat,
                line=line,
                projection=proj_val,
                opponent_abbr=opponent,
                is_home=is_home,
                ev_data=result.get("ev"),
                opponent_defense=result.get("opponentDefense"),
                matchup_history=result.get("matchupHistory"),
                reference_book_meta=result.get("referenceBook"),
                news_signals=news_signals,
            )
        except Exception as e:
            result["llmAnalysis"] = {"success": False, "error": str(e)}

    if result.get("success"):
        result["resolvedPlayerId"] = player_id
        journal_res = log_prop_ev_entry(
            result,
            player_id=player_id,
            player_identifier=argv[2],
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

        # Decision Journal signal logging
        _ls  = result.get("lineShopping") or {}
        _url = (
            _ls.get("matchedLine") is not None
            and _ls.get("bestOverOdds") is not None
        )
        _qualifies_ok, _skip = _qualifies(result, stat, used_real_line=_url)
        if _qualifies_ok:
            _ev  = result.get("ev") or {}
            _eo  = float((_ev.get("over")  or {}).get("edge") or 0.0)
            _eu  = float((_ev.get("under") or {}).get("edge") or 0.0)
            _rec = "over" if _eo >= _eu else "under"
            _book = (
                result.get("bestOverBook") if _rec == "over"
                else result.get("bestUnderBook")
            ) or "user_supplied"
            _dj  = DecisionJournal()
            _djr = _dj.log_signal(
                player_id=player_id, player_name=player_name,
                team_abbr=player_team_abbr or "", opponent_abbr=opponent,
                stat=stat, line=line, book=_book,
                over_odds=over_odds, under_odds=under_odds,
                projection=float((result.get("projection") or {}).get("projection") or 0.0),
                prob_over=float(_ev.get("probOver") or 0.0),
                prob_under=float(_ev.get("probUnder") or 0.0),
                edge_over=_eo, edge_under=_eu, recommended_side=_rec,
                recommended_edge=max(_eo, _eu),
                confidence=max(
                    float(_ev.get("probOver") or 0.0),
                    float(_ev.get("probUnder") or 0.0),
                ),
                used_real_line=bool(_url), action_taken=0,
            )
            _dj.close()
            if _djr.get("isDuplicate"):
                result["journalDuplicateSignal"] = True
            elif _djr.get("success"):
                result["journalSignalId"] = _djr.get("signalId")
            else:
                result["journalSignalError"] = _djr.get("error")
    return result


def _handle_prop_ev_ml(argv):
    if len(argv) < 9:
        return {
            "error": (
                "Usage: prop_ev_ml <player_id_or_name> <opponent_abbr> <is_home:0|1> "
                "<stat> <line> <over_odds> <under_odds> [is_b2b:0|1] [model_path]"
            )
        }
    player_id, err = resolve_player_or_result(argv[2])
    if err:
        return err
    opponent = argv[3]
    is_home = argv[4] == "1"
    stat = argv[5]
    line = float(argv[6])
    over_odds = int(argv[7])
    under_odds = int(argv[8])
    is_b2b = (argv[9] == "1") if len(argv) > 9 else False
    model_path = argv[10] if len(argv) > 10 else None
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
    return result


def _handle_auto_sweep(argv):
    if len(argv) < 7:
        return {
            "error": (
                "Usage: auto_sweep <player_id_or_name> <player_team_abbr> "
                "<opponent_abbr> <is_home:0|1> <stat> "
                "[is_b2b:0|1] [regions] [bookmakers_csv] [sport] [top_n]"
            )
        }
    player_id, err = resolve_player_or_result(argv[2])
    if err:
        return err

    player_team_abbr = str(argv[3]).upper()
    opponent = str(argv[4]).upper()
    is_home = argv[5] == "1"
    stat = argv[6]
    is_b2b = (argv[7] == "1") if len(argv) > 7 else False
    regions = argv[8] if len(argv) > 8 else "us"
    bookmakers = argv[9] if len(argv) > 9 else None
    sport = argv[10] if len(argv) > 10 else "basketball_nba"
    top_n = int(argv[11]) if len(argv) > 11 else 15

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
                player_identifier=argv[2],
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

        # Decision Journal signal logging for auto_sweep
        _best    = result.get("bestRecommendation") or {}
        _best_ev = _best.get("ev") or {}
        _qs, _   = _qualifies({"ev": _best_ev, "success": True}, stat, used_real_line=True)
        if _qs and _best_ev:
            _eo2  = float((_best_ev.get("over")  or {}).get("edge") or 0.0)
            _eu2  = float((_best_ev.get("under") or {}).get("edge") or 0.0)
            _rec2 = "over" if _eo2 >= _eu2 else "under"
            _dj2  = DecisionJournal()
            _p2   = nba_players_static.find_player_by_id(player_id)
            _dj2r = _dj2.log_signal(
                player_id=player_id,
                player_name=(_p2.get("full_name") if _p2 else str(argv[2])),
                team_abbr=player_team_abbr, opponent_abbr=opponent,
                stat=stat, line=float(_best.get("line") or 0.0),
                book=str(_best.get("bookmaker") or ""),
                over_odds=int(_best.get("overOdds") or -110),
                under_odds=int(_best.get("underOdds") or -110),
                projection=float((result.get("projection") or {}).get("projection") or 0.0),
                prob_over=float(_best_ev.get("probOver") or 0.0),
                prob_under=float(_best_ev.get("probUnder") or 0.0),
                edge_over=_eo2, edge_under=_eu2, recommended_side=_rec2,
                recommended_edge=max(_eo2, _eu2),
                confidence=max(
                    float(_best_ev.get("probOver") or 0.0),
                    float(_best_ev.get("probUnder") or 0.0),
                ),
                used_real_line=True,  # auto_sweep always uses live Odds API lines
                action_taken=0,
            )
            _dj2.close()
            if _dj2r.get("isDuplicate"):
                result["journalDuplicateSignal"] = True
            elif _dj2r.get("success"):
                result["journalSignalId"] = _dj2r.get("signalId")
    return result


def _handle_parlay_ev(argv):
    if len(argv) < 3:
        return {
            "error": (
                "Usage: parlay_ev '<json_legs>' where json_legs is a JSON array. "
                "Each leg needs: playerId, playerTeam, stat, line, side, "
                "probOver, overOdds, underOdds. 2-3 legs supported."
            )
        }
    try:
        legs = json.loads(argv[2])
    except json.JSONDecodeError as je:
        return {"error": f"Invalid JSON for parlay legs: {je}"}
    if not isinstance(legs, list):
        return {"error": "parlay_ev argument must be a JSON array of legs"}
    return compute_parlay_ev(legs)


_COMMANDS = {
    "prop_ev":    _handle_prop_ev,
    "prop_ev_ml": _handle_prop_ev_ml,
    "auto_sweep": _handle_auto_sweep,
    "parlay_ev":  _handle_parlay_ev,
}


def handle_ev_command(command, argv):  # shim — router no longer calls this
    h = _COMMANDS.get(command)
    return h(argv) if h else None
