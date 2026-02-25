#!/usr/bin/env python3
"""Single-prop evaluation and auto line sweep."""

from nba_api.stats.static import players as nba_players_static

from nba_data_collection import get_nba_player_prop_offers, safe_round
from nba_data_prep import compute_projection
from nba_ev_engine import american_to_decimal, compute_ev

_ALL_PLAYERS_BY_ID = {
    int(p["id"]): str(p.get("full_name", ""))
    for p in nba_players_static.get_players()
    if p.get("id")
}


def _best_side_prices_for_line(offers, target_line, tolerance=0.05):
    best_over = None
    best_under = None
    for offer in offers or []:
        try:
            line_val = float(offer.get("line"))
        except (TypeError, ValueError):
            continue
        if abs(line_val - float(target_line)) > tolerance:
            continue

        over_odds = offer.get("overOdds")
        under_odds = offer.get("underOdds")
        over_dec = american_to_decimal(over_odds)
        under_dec = american_to_decimal(under_odds)

        if over_dec is not None:
            if best_over is None or over_dec > best_over["decimal"]:
                best_over = {
                    "odds": int(over_odds),
                    "book": offer.get("bookmaker"),
                    "decimal": over_dec,
                    "line": line_val,
                }
        if under_dec is not None:
            if best_under is None or under_dec > best_under["decimal"]:
                best_under = {
                    "odds": int(under_odds),
                    "book": offer.get("bookmaker"),
                    "decimal": under_dec,
                    "line": line_val,
                }
    return best_over, best_under


def compute_prop_ev(
    player_id,
    opponent_abbr,
    is_home,
    stat,
    line,
    over_odds,
    under_odds,
    is_b2b=False,
    season=None,
    player_team_abbr=None,
    regions="us",
    bookmakers=None,
    sport="basketball_nba",
    model_variant="full",
):
    stat_key = str(stat or "").lower().strip()
    line_val = float(line)

    proj_data = compute_projection(
        player_id=player_id,
        opponent_abbr=opponent_abbr,
        is_home=is_home,
        is_b2b=is_b2b,
        season=season,
        blend_with_line={stat_key: line_val},
        model_variant=model_variant,
    )
    if not proj_data.get("success"):
        return proj_data

    proj = (proj_data.get("projections") or {}).get(stat_key)
    if not proj:
        return {
            "success": False,
            "error": f"No projection available for stat '{stat_key}'. "
                     f"Valid stats: {list((proj_data.get('projections') or {}).keys())}",
        }

    best_over_odds = int(over_odds)
    best_under_odds = int(under_odds)
    best_over_book = None
    best_under_book = None
    line_shopping = None

    if player_team_abbr:
        player_name = _ALL_PLAYERS_BY_ID.get(int(player_id), "")
        if player_name:
            offers_data = get_nba_player_prop_offers(
                player_name=player_name,
                player_team_abbr=player_team_abbr,
                opponent_abbr=opponent_abbr,
                is_home=is_home,
                stat=stat_key,
                regions=regions,
                bookmakers=bookmakers,
                sport=sport,
                odds_format="american",
            )
            if offers_data.get("success"):
                offers = offers_data.get("offers", []) or []
                best_over, best_under = _best_side_prices_for_line(offers, line_val, tolerance=0.051)
                if best_over:
                    best_over_odds = int(best_over["odds"])
                    best_over_book = best_over.get("book")
                if best_under:
                    best_under_odds = int(best_under["odds"])
                    best_under_book = best_under.get("book")
                line_shopping = {
                    "eventId": offers_data.get("eventId"),
                    "eventHomeTeam": offers_data.get("eventHomeTeam"),
                    "eventAwayTeam": offers_data.get("eventAwayTeam"),
                    "offerCount": len(offers),
                    "matchedLine": line_val,
                    "bestOverOdds": best_over_odds,
                    "bestOverBook": best_over_book,
                    "bestUnderOdds": best_under_odds,
                    "bestUnderBook": best_under_book,
                    "quota": offers_data.get("quota"),
                    "discoveryQuota": offers_data.get("discoveryQuota"),
                }
            else:
                line_shopping = {
                    "error": offers_data.get("error"),
                    "details": offers_data.get("details"),
                }

    projection_val = proj["projection"]
    stdev_val = proj.get("projStdev") or proj.get("stdev") or 0
    ev_data = compute_ev(projection_val, line_val, best_over_odds, best_under_odds, stdev_val)

    return {
        "success": True,
        "stat": stat_key,
        "line": line_val,
        "projection": proj,
        "ev": ev_data,
        "matchupHistory": proj_data.get("matchupHistory"),
        "opponentDefense": proj_data.get("opponentDefense"),
        "position": proj_data.get("position"),
        "gamesPlayed": proj_data.get("gamesPlayed"),
        "playerId": player_id,
        "opponent": opponent_abbr,
        "isHome": is_home,
        "isB2B": is_b2b,
        "modelVariant": proj_data.get("modelVariant"),
        "bestOverOdds": best_over_odds,
        "bestUnderOdds": best_under_odds,
        "bestOverBook": best_over_book,
        "bestUnderBook": best_under_book,
        "lineShopping": line_shopping,
    }


def compute_auto_line_sweep(
    player_id,
    player_team_abbr,
    opponent_abbr,
    is_home,
    stat,
    is_b2b=False,
    season=None,
    regions="us",
    bookmakers=None,
    sport="basketball_nba",
    top_n=15,
):
    try:
        proj_data = compute_projection(player_id, opponent_abbr, is_home, is_b2b, season)
        if not proj_data.get("success"):
            return proj_data

        stat_key = str(stat or "").lower().strip()
        proj = (proj_data.get("projections") or {}).get(stat_key)
        if not proj:
            return {
                "success": False,
                "error": f"No projection available for stat '{stat_key}'.",
                "validStats": list((proj_data.get("projections") or {}).keys()),
            }

        player_name = _ALL_PLAYERS_BY_ID.get(int(player_id), "")
        if not player_name:
            return {"success": False, "error": f"Could not resolve player name for player_id={player_id}"}

        offer_data = get_nba_player_prop_offers(
            player_name=player_name,
            player_team_abbr=player_team_abbr,
            opponent_abbr=opponent_abbr,
            is_home=is_home,
            stat=stat_key,
            regions=regions,
            bookmakers=bookmakers,
            sport=sport,
            odds_format="american",
        )
        if not offer_data.get("success"):
            return {
                "success": False,
                "error": offer_data.get("error", "Failed to fetch player prop offers."),
                "details": offer_data.get("details"),
                "projection": proj,
                "playerId": player_id,
                "playerName": player_name,
                "stat": stat_key,
            }

        offers = offer_data.get("offers", []) or []
        if not offers:
            return {
                "success": False,
                "error": "No over/under line pairs found for this player/stat in selected books.",
                "projection": proj,
                "playerId": player_id,
                "playerName": player_name,
                "stat": stat_key,
                "offerCount": 0,
                "eventId": offer_data.get("eventId"),
                "eventHomeTeam": offer_data.get("eventHomeTeam"),
                "eventAwayTeam": offer_data.get("eventAwayTeam"),
            }

        projection_val = proj.get("projection")
        stdev_val = proj.get("projStdev") or proj.get("stdev") or 0
        ranked = []
        for offer in offers:
            line = offer.get("line")
            over_odds = offer.get("overOdds")
            under_odds = offer.get("underOdds")
            if line is None or over_odds is None or under_odds is None:
                continue

            ev = compute_ev(projection_val, line, over_odds, under_odds, stdev_val)
            if not ev:
                continue

            over_ev = (ev.get("over") or {}).get("evPercent")
            under_ev = (ev.get("under") or {}).get("evPercent")
            best_side = "over"
            best_ev_pct = over_ev if over_ev is not None else -9999
            if under_ev is not None and (best_ev_pct is None or under_ev > best_ev_pct):
                best_side = "under"
                best_ev_pct = under_ev

            ranked.append(
                {
                    "bookmaker": offer.get("bookmaker"),
                    "line": line,
                    "overOdds": over_odds,
                    "underOdds": under_odds,
                    "bestSide": best_side,
                    "bestEvPct": safe_round(best_ev_pct, 2) if best_ev_pct is not None else None,
                    "evOverPct": safe_round(over_ev, 2) if over_ev is not None else None,
                    "evUnderPct": safe_round(under_ev, 2) if under_ev is not None else None,
                    "probOver": ev.get("probOver"),
                    "probUnder": ev.get("probUnder"),
                    "edgeOver": (ev.get("over") or {}).get("edge"),
                    "edgeUnder": (ev.get("under") or {}).get("edge"),
                    "overVerdict": (ev.get("over") or {}).get("verdict"),
                    "underVerdict": (ev.get("under") or {}).get("verdict"),
                    "ev": ev,
                }
            )

        if not ranked:
            return {
                "success": False,
                "error": "No lines could be scored for EV from available offers.",
                "projection": proj,
                "playerId": player_id,
                "playerName": player_name,
                "stat": stat_key,
                "offerCount": len(offers),
            }

        ranked.sort(
            key=lambda x: (x.get("bestEvPct", -9999), -abs((x.get("line") or 0) - (projection_val or 0))),
            reverse=True,
        )
        top_n_val = max(1, min(200, int(top_n or 15)))
        ranked_top = ranked[:top_n_val]
        best = ranked_top[0]

        return {
            "success": True,
            "playerId": player_id,
            "playerName": player_name,
            "playerTeamAbbr": str(player_team_abbr or "").upper(),
            "opponentAbbr": str(opponent_abbr or "").upper(),
            "isHome": bool(is_home),
            "isB2B": bool(is_b2b),
            "stat": stat_key,
            "projection": proj,
            "projectionValue": projection_val,
            "marketKey": offer_data.get("marketKey"),
            "eventId": offer_data.get("eventId"),
            "eventHomeTeam": offer_data.get("eventHomeTeam"),
            "eventAwayTeam": offer_data.get("eventAwayTeam"),
            "commenceTime": offer_data.get("commenceTime"),
            "offerCount": len(ranked),
            "rankedOffers": ranked_top,
            "bestRecommendation": best,
            "quota": offer_data.get("quota"),
            "discoveryQuota": offer_data.get("discoveryQuota"),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
