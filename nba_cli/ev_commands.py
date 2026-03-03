#!/usr/bin/env python3
"""EV CLI commands: prop_ev, prop_ev_ml, auto_sweep, parlay_ev."""

import json
from datetime import datetime, timezone

from nba_api.stats.static import players as nba_players_static

from core.nba_bet_tracking import log_prop_ev_entry
from core.nba_decision_journal import DecisionJournal, _qualifies
from core.nba_data_collection import safe_round
from core.nba_data_prep import compute_usage_adjustment
from core.nba_prep_projection import _SHRINK_K
from core.nba_injury_news import fetch_nba_injury_news
from core.nba_llm_engine import llm_full_analysis
from core.nba_model_training import (
    american_to_implied_prob,
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


def _compute_intraday_clv(player_name, stat, book, recommended_side):
    """
    Compare the opening vs current LineStore snapshot for this player/stat/book.
    Returns the intraday CLV line float (positive = line moved in our favour),
    or None if fewer than 2 distinct timestamps exist or LineStore has no data.
    """
    try:
        from core.nba_line_store import LineStore
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        book_hint = book if book != "user_supplied" else None
        ls = LineStore()
        all_snaps = ls.get_snapshots(today_str, book=book_hint, stat=stat, player_name=player_name)
        distinct_ts = {s.get("timestamp_utc") for s in all_snaps if s.get("timestamp_utc")}
        if len(distinct_ts) < 2:
            return None
        opening = min(all_snaps, key=lambda x: x.get("timestamp_utc", ""))
        current = max(all_snaps, key=lambda x: x.get("timestamp_utc", ""))
        o_line = opening.get("line")
        c_line = current.get("line")
        if o_line is None or c_line is None:
            return None
        # Positive = line moved in our favour (lower for over, higher for under)
        if recommended_side == "over":
            return round(float(c_line) - float(o_line), 2)
        return round(float(o_line) - float(c_line), 2)
    except Exception:
        return None


def _build_signal_context(source, result, stat, stat_proj, intraday_clv=None):
    """Build the context dict passed to DecisionJournal.log_signal."""
    ctx = {"source": source}
    ref_book = result.get("referenceBook")
    if ref_book:
        ctx["referenceBook"] = ref_book
    recent_hv = stat_proj.get("recentHighVariance")
    if recent_hv is not None:
        ctx["recentHighVariance"] = recent_hv
    if intraday_clv is not None:
        ctx["intradayClvLine"] = intraday_clv
    ctx["shrink_k"] = _SHRINK_K
    return ctx


def _log_dj_signal(result, player_id, player_name, team_abbr, opponent_abbr,
                   stat, line, over_odds, under_odds, book, stat_proj,
                   ev_data, used_real_line, context):
    """
    Log a qualifying signal to the DecisionJournal and annotate result with
    journalSignalId / journalDuplicateSignal / journalSignalError.
    """
    eo = float((ev_data.get("over") or {}).get("edge") or 0.0)
    eu = float((ev_data.get("under") or {}).get("edge") or 0.0)
    rec = "over" if eo >= eu else "under"
    dj = DecisionJournal()
    djr = dj.log_signal(
        player_id=player_id, player_name=player_name,
        team_abbr=team_abbr or "", opponent_abbr=opponent_abbr,
        stat=stat, line=line, book=book,
        over_odds=over_odds, under_odds=under_odds,
        projection=float(stat_proj.get("projection") or 0.0),
        prob_over=float(ev_data.get("probOver") or 0.0),
        prob_under=float(ev_data.get("probUnder") or 0.0),
        edge_over=eo, edge_under=eu, recommended_side=rec,
        recommended_edge=max(eo, eu),
        confidence=max(
            float(ev_data.get("probOver") or 0.0),
            float(ev_data.get("probUnder") or 0.0),
        ),
        used_real_line=used_real_line, action_taken=0,
        context=context,
    )
    dj.close()
    if djr.get("isDuplicate"):
        result["journalDuplicateSignal"] = True
    elif djr.get("success"):
        result["journalSignalId"] = djr.get("signalId")
    else:
        result["journalSignalError"] = djr.get("error")


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

    no_blend = "--no-blend" in argv

    # Extract --mins-mult <value> flag if present
    _mins_mult = None
    _filtered_argv = []
    _skip = False
    for i, a in enumerate(argv):
        if _skip:
            _skip = False
            continue
        if a == "--mins-mult" and i + 1 < len(argv):
            try:
                _mins_mult = float(argv[i + 1])
            except (ValueError, TypeError):
                pass
            _skip = True
            continue
        if a == "--no-blend":
            continue
        _filtered_argv.append(a)
    clean_argv = _filtered_argv

    opponent = clean_argv[3]
    is_home = clean_argv[4] == "1"
    stat = clean_argv[5]
    line = float(clean_argv[6])
    over_odds = int(clean_argv[7])
    under_odds = int(clean_argv[8])
    is_b2b = (clean_argv[9] == "1") if len(clean_argv) > 9 else False
    player_team_abbr = clean_argv[10] if len(clean_argv) > 10 else None
    reference_book = clean_argv[11] if len(clean_argv) > 11 else None
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
        no_blend=no_blend,
        minutes_multiplier=_mins_mult,
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
        line_shopping = result.get("lineShopping") or {}
        used_real_line = (
            line_shopping.get("matchedLine") is not None
            and line_shopping.get("bestOverOdds") is not None
        )
        qualifies_ok, _skip = _qualifies(result, stat, used_real_line=used_real_line)
        if qualifies_ok:
            ev_data = result.get("ev") or {}
            eo = float((ev_data.get("over") or {}).get("edge") or 0.0)
            eu = float((ev_data.get("under") or {}).get("edge") or 0.0)
            rec = "over" if eo >= eu else "under"
            book = (
                result.get("bestOverBook") if rec == "over"
                else result.get("bestUnderBook")
            ) or "user_supplied"

            intraday_clv = _compute_intraday_clv(player_name, stat, book, rec)
            if intraday_clv is not None:
                result["intradayClvLine"] = intraday_clv

            stat_proj = (result.get("projections") or {}).get(stat) or result.get("projection") or {}
            ctx = _build_signal_context("prop_ev", result, stat, stat_proj, intraday_clv)
            _log_dj_signal(
                result=result,
                player_id=player_id, player_name=player_name,
                team_abbr=player_team_abbr, opponent_abbr=opponent,
                stat=stat, line=line, book=book,
                over_odds=over_odds, under_odds=under_odds,
                stat_proj=stat_proj, ev_data=ev_data,
                used_real_line=bool(used_real_line), context=ctx,
            )
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
        best_ev_data = (result.get("bestRecommendation") or {}).get("ev") or {}
        stat_proj = (result.get("projections") or {}).get(stat) or result.get("projection") or {}

        # Extract Pinnacle no-vig from rankedOffers if available — no extra API call needed.
        # Populates referenceBook so the Pinnacle confirmation gate fires when Pinnacle
        # was one of the swept books. Falls back to None (gate skipped) if absent.
        _ref_book = None
        for _offer in (result.get("rankedOffers") or []):
            if str(_offer.get("bookmaker") or "").lower() == "pinnacle":
                _po = american_to_implied_prob(_offer.get("overOdds"))
                _pu = american_to_implied_prob(_offer.get("underOdds"))
                if _po and _pu and (_po + _pu) > 0:
                    _t = _po + _pu
                    _ref_book = {
                        "book": "pinnacle",
                        "line": _offer.get("line"),
                        "overOdds": _offer.get("overOdds"),
                        "underOdds": _offer.get("underOdds"),
                        "noVigOver": safe_round(_po / _t, 4),
                        "noVigUnder": safe_round(_pu / _t, 4),
                    }
                break

        sweep_qual_result = {
            "ev": best_ev_data,
            "success": True,
            "referenceBook": _ref_book,
            "projection": stat_proj,
            "minutesProjection": result.get("minutesProjection"),
            "nBooksOffering": result.get("nBooksOffering"),
        }
        qualifies_ok, _ = _qualifies(sweep_qual_result, stat, used_real_line=True)
        if qualifies_ok and best_ev_data:
            p = nba_players_static.find_player_by_id(player_id)
            player_name = p.get("full_name") if p else str(argv[2])
            ctx = _build_signal_context("auto_sweep", result, stat, stat_proj)
            _log_dj_signal(
                result=result,
                player_id=player_id, player_name=player_name,
                team_abbr=player_team_abbr, opponent_abbr=opponent,
                stat=stat, line=float(best.get("line") or 0.0),
                book=str(best.get("bookmaker") or ""),
                over_odds=int(best.get("overOdds") or -110),
                under_odds=int(best.get("underOdds") or -110),
                stat_proj=stat_proj, ev_data=best_ev_data,
                used_real_line=True,  # auto_sweep always uses live Odds API lines
                context=ctx,
            )
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
