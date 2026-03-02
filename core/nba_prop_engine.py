#!/usr/bin/env python3
"""Single-prop evaluation and auto line sweep."""

from nba_api.stats.static import players as nba_players_static

from .nba_data_collection import get_nba_player_prop_offers, safe_round
from .nba_data_prep import compute_projection
from .nba_ev_engine import american_to_decimal, american_to_implied_prob, compute_ev

_ALL_PLAYERS_BY_ID = {
    int(p["id"]): str(p.get("full_name", ""))
    for p in nba_players_static.get_players()
    if p.get("id")
}
PREFERRED_BOOKMAKERS = ("betmgm", "draftkings", "fanduel")


def _normalize_book_key(book_name):
    return "".join(ch for ch in str(book_name or "").lower() if ch.isalnum())


def _book_priority_score(book_name):
    key = _normalize_book_key(book_name)
    if "betmgm" in key:
        return 3
    if "draftkings" in key:
        return 2
    if "fanduel" in key:
        return 1
    return 0


def _clean_bookmaker_csv(raw_value, default_csv=None):
    items = [x.strip().lower() for x in str(raw_value or "").split(",") if x.strip()]
    if items:
        return ",".join(items)
    return str(default_csv or "").strip().lower()


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
            if (
                best_over is None
                or over_dec > best_over["decimal"]
                or (
                    abs(over_dec - best_over["decimal"]) < 1e-9
                    and _book_priority_score(offer.get("bookmaker")) > _book_priority_score(best_over.get("book"))
                )
            ):
                best_over = {
                    "odds": int(over_odds),
                    "book": offer.get("bookmaker"),
                    "decimal": over_dec,
                    "line": line_val,
                }
        if under_dec is not None:
            if (
                best_under is None
                or under_dec > best_under["decimal"]
                or (
                    abs(under_dec - best_under["decimal"]) < 1e-9
                    and _book_priority_score(offer.get("bookmaker")) > _book_priority_score(best_under.get("book"))
                )
            ):
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
    reference_book=None,
    auto_pinnacle=True,
    no_blend=False,
):
    stat_key = str(stat or "").lower().strip()
    line_val = float(line)
    preferred_books_csv = ",".join(PREFERRED_BOOKMAKERS)
    books_filter = _clean_bookmaker_csv(bookmakers, default_csv=preferred_books_csv)

    # #7: Auto-Pinnacle — include pinnacle in the same fetch so we get its no-vig
    # probability as a reference in one API call (no extra credit cost).
    # Only added when the user hasn't specified an explicit reference_book.
    _auto_pin = auto_pinnacle and not reference_book
    if _auto_pin:
        _existing_books = {_normalize_book_key(b) for b in books_filter.split(",") if b.strip()}
        if "pinnacle" not in _existing_books:
            books_filter_for_fetch = books_filter + ",pinnacle"
        else:
            books_filter_for_fetch = books_filter
    else:
        books_filter_for_fetch = books_filter

    proj_data = compute_projection(
        player_id=player_id,
        opponent_abbr=opponent_abbr,
        is_home=is_home,
        is_b2b=is_b2b,
        season=season,
        blend_with_line=None if no_blend else {stat_key: line_val},
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
    player_name = _ALL_PLAYERS_BY_ID.get(int(player_id), "")
    reference_probs = None
    reference_book_meta = None

    if player_team_abbr and player_name:
        offers_data = get_nba_player_prop_offers(
            player_name=player_name,
            player_team_abbr=player_team_abbr,
            opponent_abbr=opponent_abbr,
            is_home=is_home,
            stat=stat_key,
            regions=regions,
            bookmakers=books_filter_for_fetch,
            sport=sport,
            odds_format="american",
        )
        if offers_data.get("success"):
            offers = offers_data.get("offers", []) or []

            # #7: Auto-Pinnacle reference — extract Pinnacle offers within ±0.5 of
            # target line and use their no-vig probability instead of the Normal CDF.
            # Pinnacle is the sharpest book; their implied prob is more reliable than
            # the model's 70% that actually hits at 40%.
            if _auto_pin:
                pin_offers = [
                    o for o in offers
                    if _normalize_book_key(o.get("bookmaker", "")) == "pinnacle"
                ]
                pin_over, pin_under = _best_side_prices_for_line(
                    pin_offers, line_val, tolerance=0.501
                )
                if pin_over and pin_under:
                    po = american_to_implied_prob(pin_over["odds"])
                    pu = american_to_implied_prob(pin_under["odds"])
                    total = (po or 0.0) + (pu or 0.0)
                    if total > 0:
                        reference_probs = {
                            "over": po / total,
                            "under": pu / total,
                            "push": 0.0,
                        }
                        pin_line = float(pin_over.get("line", line_val))
                        reference_book_meta = {
                            "book": "pinnacle",
                            "line": pin_line,
                            "overOdds": pin_over["odds"],
                            "underOdds": pin_under["odds"],
                            "noVigOver": safe_round(po / total, 4),
                            "noVigUnder": safe_round(pu / total, 4),
                            "auto": True,
                            "lineDelta": safe_round(abs(pin_line - line_val), 2),
                        }

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

        if reference_book and str(reference_book).strip():
            ref_book_str = str(reference_book).strip()
            ref_offers_data = get_nba_player_prop_offers(
                player_name=player_name,
                player_team_abbr=player_team_abbr,
                opponent_abbr=opponent_abbr,
                is_home=is_home,
                stat=stat_key,
                regions=regions,
                bookmakers=ref_book_str,
                sport=sport,
                odds_format="american",
            )
            if ref_offers_data.get("success"):
                ref_offers = ref_offers_data.get("offers", []) or []
                ref_over, ref_under = _best_side_prices_for_line(ref_offers, line_val, tolerance=0.051)
                if ref_over and ref_under:
                    p_over_raw = american_to_implied_prob(ref_over["odds"])
                    p_under_raw = american_to_implied_prob(ref_under["odds"])
                    total = (p_over_raw or 0.0) + (p_under_raw or 0.0)
                    if total > 0:
                        reference_probs = {
                            "over": p_over_raw / total,
                            "under": p_under_raw / total,
                            "push": 0.0,
                        }
                        reference_book_meta = {
                            "book": ref_book_str,
                            "line": line_val,
                            "overOdds": ref_over["odds"],
                            "underOdds": ref_under["odds"],
                            "noVigOver": safe_round(p_over_raw / total, 4),
                            "noVigUnder": safe_round(p_under_raw / total, 4),
                        }

    projection_val = proj["projection"]
    stdev_val = proj.get("projStdev") or proj.get("stdev") or 0
    ev_data = compute_ev(
        projection_val, line_val, best_over_odds, best_under_odds, stdev_val,
        stat=stat_key, reference_probs=reference_probs,
    )

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
        "minutesProjection": proj_data.get("minutesProjection"),
        "bestOverOdds": best_over_odds,
        "bestUnderOdds": best_under_odds,
        "bestOverBook": best_over_book,
        "bestUnderBook": best_under_book,
        "lineShopping": line_shopping,
        "referenceBook": reference_book_meta,
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
    allow_book_fallback=False,
):
    try:
        requested_books = _clean_bookmaker_csv(bookmakers, default_csv=",".join(PREFERRED_BOOKMAKERS))

        def _fetch_offers(bookmakers_filter):
            return get_nba_player_prop_offers(
                player_name=player_name,
                player_team_abbr=player_team_abbr,
                opponent_abbr=opponent_abbr,
                is_home=is_home,
                stat=stat_key,
                regions=regions,
                bookmakers=bookmakers_filter,
                sport=sport,
                odds_format="american",
            )

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

        fallback_used = False
        fallback_reason = None
        offer_data = _fetch_offers(requested_books)
        if not offer_data.get("success"):
            # If selected books fail (often due pulled markets), retry across all books.
            if requested_books and allow_book_fallback:
                fallback_data = _fetch_offers(None)
                if fallback_data.get("success"):
                    fallback_used = True
                    fallback_reason = "selected_bookmakers_failed"
                    offer_data = fallback_data
                else:
                    return {
                        "success": False,
                        "error": offer_data.get("error", "Failed to fetch player prop offers."),
                        "details": offer_data.get("details"),
                        "projection": proj,
                        "playerId": player_id,
                        "playerName": player_name,
                        "stat": stat_key,
                        "fallbackAttempted": True,
                        "fallbackSucceeded": False,
                        "fallbackError": fallback_data.get("error"),
                        "fallbackDetails": fallback_data.get("details"),
                    }
            else:
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
        if not offers and requested_books and allow_book_fallback:
            # Selected books had no complete over/under pairs, retry all books.
            fallback_data = _fetch_offers(None)
            fallback_offers = fallback_data.get("offers", []) or []
            if fallback_data.get("success") and fallback_offers:
                fallback_used = True
                fallback_reason = "selected_bookmakers_no_pairs"
                offer_data = fallback_data
                offers = fallback_offers
            elif fallback_data.get("success"):
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
                    "fallbackAttempted": True,
                    "fallbackSucceeded": False,
                }
            else:
                return {
                    "success": False,
                    "error": "No over/under line pairs found for this player/stat in selected books.",
                    "details": fallback_data.get("error"),
                    "projection": proj,
                    "playerId": player_id,
                    "playerName": player_name,
                    "stat": stat_key,
                    "offerCount": 0,
                    "eventId": offer_data.get("eventId"),
                    "eventHomeTeam": offer_data.get("eventHomeTeam"),
                    "eventAwayTeam": offer_data.get("eventAwayTeam"),
                    "fallbackAttempted": True,
                    "fallbackSucceeded": False,
                }

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

        # Gap 8.12 + 8.8: compute cross-offer statistics once before the loop.
        # These are per-player/stat aggregates, not per-offer values.
        import statistics as _statistics
        _all_lines = [
            float(o["line"]) for o in offers
            if o.get("line") is not None
        ]
        book_line_stdev = (
            safe_round(_statistics.stdev(_all_lines), 3)
            if len(_all_lines) >= 2
            else 0.0
        )
        # Gap 8.8: count unique bookmakers across all offers for this player/stat
        n_books_offering = len(set(
            o.get("bookmaker") for o in offers if o.get("bookmaker")
        ))

        # Gap 8.12: stale consensus heuristic — compute median vig and median line
        # across all valid offers for staleConsensus per-offer flag.
        _all_vigs = []
        for _o in offers:
            _oi = american_to_implied_prob(_o.get("overOdds")) or 0.5
            _ui = american_to_implied_prob(_o.get("underOdds")) or 0.5
            _all_vigs.append(_oi + _ui - 1.0)
        _median_vig = _statistics.median(_all_vigs) if _all_vigs else None
        _median_line = _statistics.median(_all_lines) if _all_lines else None

        # Gap 8.16: Cross-book line dispersion — identify outlier books whose line
        # deviates from the median by > 0.4. A soft line is an exploitable edge.
        # Informational: stored in result for context, not a blocking signal.
        soft_line_books = (
            [
                o.get("bookmaker") for o in offers
                if o.get("line") is not None and _median_line is not None
                and abs(float(o["line"]) - _median_line) > 0.4
            ]
            if _median_line is not None
            else []
        )

        ranked = []
        for offer in offers:
            line = offer.get("line")
            over_odds = offer.get("overOdds")
            under_odds = offer.get("underOdds")
            if line is None or over_odds is None or under_odds is None:
                continue

            ev = compute_ev(projection_val, line, over_odds, under_odds, stdev_val, stat=stat_key)
            if not ev:
                continue

            over_ev = (ev.get("over") or {}).get("evPercent")
            under_ev = (ev.get("under") or {}).get("evPercent")
            best_side = "over"
            best_ev_pct = over_ev if over_ev is not None else -9999
            if under_ev is not None and (best_ev_pct is None or under_ev > best_ev_pct):
                best_side = "under"
                best_ev_pct = under_ev

            # Gap 8.7: vig asymmetry — lower vig books have tighter spreads → higher EV
            # vigSpread = sum of implied probs − 1.0; lower is better (less juice)
            _over_imp = american_to_implied_prob(over_odds) or 0.5
            _under_imp = american_to_implied_prob(under_odds) or 0.5
            vig_spread = safe_round(_over_imp + _under_imp - 1.0, 4)

            # Gap 8.12: stale consensus flag per offer.
            # True when all books quote within 0.002 vig of each other AND within 0.25
            # of the median line — indicates no sharp action / stale market.
            if _median_vig is not None and _median_line is not None:
                _vig_close = abs((_over_imp + _under_imp - 1.0) - _median_vig) <= 0.002
                _line_close = abs(float(line) - _median_line) <= 0.25
                stale_consensus = _vig_close and _line_close
            else:
                stale_consensus = False

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
                    "vigSpread": vig_spread,
                    "staleConsensus": stale_consensus,
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
            key=lambda x: (
                x.get("bestEvPct", -9999),
                -(x.get("vigSpread") or 0.10),   # lower vig → less negative → ranks higher
                -abs((x.get("line") or 0) - (projection_val or 0)),
                _book_priority_score(x.get("bookmaker")),
            ),
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
            "minutesProjection": proj_data.get("minutesProjection"),
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
            "requestedBookmakers": requested_books or None,
            "fallbackUsed": fallback_used,
            "fallbackReason": fallback_reason,
            "bookmakersRequested": offer_data.get("bookmakersRequested"),
            # Gap 8.8: number of unique books posting this prop (across all offers)
            "nBooksOffering": n_books_offering,
            # Gap 8.12: stdev of lines across all books (0.0 = identical lines)
            "bookLineStdev": book_line_stdev,
            # Gap 8.16: books whose line deviates from median by > 0.4 (soft line signal)
            "softLineBooks": soft_line_books,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


_STAT_BOX_KEY = {
    "pts": "PTS", "reb": "REB", "ast": "AST",
    "stl": "STL", "blk": "BLK", "tov": "TOV",
    "fg3m": "FG3M", "pra": "PRA",
}


def _clamp(val, lo, hi):
    return max(lo, min(hi, val))


def compute_live_projection(pregame_proj, live_stats, stat_key):
    """
    Adjust a pregame projection using current in-game stats.

    Formula:
        remaining_mins      = projectedMinutes - mins_played
        blended_per_min     = blend(pregame_per_min, live_per_min, sample_weight)
        projected_remaining = blended_per_min * adjusted_remaining_mins
        live_projection   = current_stat + projected_remaining
    """
    try:
        stat_key = str(stat_key).lower().strip()
        box_key = _STAT_BOX_KEY.get(stat_key, stat_key.upper())

        per_min_rate = float(pregame_proj.get("perMinRate") or 0)
        projected_minutes = float(pregame_proj.get("projectedMinutes") or 36)
        pregame_projection = float(pregame_proj.get("projection") or 0)

        current_stat = float(live_stats.get(box_key) or 0)
        mins_played = float(live_stats.get("minsPlayed") or 0)

        remaining_mins = max(0.0, projected_minutes - mins_played)
        progress = mins_played / projected_minutes if projected_minutes > 0 else 0.0
        live_per_min = current_stat / mins_played if mins_played > 0 else per_min_rate

        # Move from pregame-driven to live-driven as minutes accumulate.
        blend_weight = _clamp(progress * 0.55, 0.0, 0.55)
        shot_attempts = float(live_stats.get("ShotAttempts") or 0.0)
        if shot_attempts <= 0:
            fga = float(live_stats.get("FGA") or 0.0)
            fta = float(live_stats.get("FTA") or 0.0)
            shot_attempts = fga + 0.44 * fta

        # For scoring stats, use shot volume as additional confidence in live rate.
        if stat_key in {"pts", "fg3m"}:
            shot_weight = _clamp(shot_attempts / 20.0, 0.0, 0.65)
            blend_weight = max(blend_weight, shot_weight)

        blended_per_min = (1.0 - blend_weight) * per_min_rate + blend_weight * live_per_min
        if per_min_rate > 0:
            blended_per_min = _clamp(blended_per_min, per_min_rate * 0.55, per_min_rate * 1.45)
        blended_per_min = max(0.0, blended_per_min)

        # Minutes context: slight boost in close games, slight trim in blowouts/foul trouble.
        minute_multiplier = 1.0
        period = int(live_stats.get("period") or 0)
        margin_abs = abs(float(live_stats.get("scoreMargin") or 0.0))
        fouls = float(live_stats.get("PF") or 0.0)
        if period >= 3:
            if margin_abs <= 6:
                minute_multiplier *= 1.06
            elif margin_abs >= 15:
                minute_multiplier *= 0.90
            if fouls >= 5:
                minute_multiplier *= 0.90
        elif period == 2 and margin_abs <= 4:
            minute_multiplier *= 1.02

        adjusted_remaining_mins = max(0.0, remaining_mins * minute_multiplier)

        # Close-game minutes floor: the pregame soft cap (33 min + decay)
        # compresses star minutes to ~34 min for load-management risk.
        # Once we're live and the game is close, override that ceiling
        # using regulation time remaining and the player's usage share
        # of game minutes.  Only for starter-level players (28+ min avg).
        close_game_floor_applied = False
        if projected_minutes >= 28 and period >= 3 and margin_abs <= 10 and fouls < 5:
            _REGULATION_MINS_BY_PERIOD = {3: 24.0, 4: 12.0}
            reg_remaining = _REGULATION_MINS_BY_PERIOD.get(period, 0.0)
            if period > 4:
                reg_remaining = 5.0
            usage_share = min(0.88, projected_minutes / 48.0)
            close_game_floor = reg_remaining * usage_share
            if close_game_floor > adjusted_remaining_mins:
                adjusted_remaining_mins = close_game_floor
                close_game_floor_applied = True

        projected_remaining = blended_per_min * adjusted_remaining_mins
        live_projection = safe_round(current_stat + projected_remaining, 1)

        pace_pct = safe_round(mins_played / projected_minutes * 100, 1) if projected_minutes > 0 else 0

        return {
            "success": True,
            "stat": stat_key,
            "liveProjection": live_projection,
            "currentStat": current_stat,
            "minsPlayed": safe_round(mins_played, 1),
            "remainingMins": safe_round(adjusted_remaining_mins, 1),
            "projectedMinutes": projected_minutes,
            "perMinRate": safe_round(blended_per_min, 4),
            "basePerMinRate": safe_round(per_min_rate, 4),
            "livePerMinRate": safe_round(live_per_min, 4),
            "blendWeight": safe_round(blend_weight, 3),
            "minuteMultiplier": safe_round(minute_multiplier, 3),
            "closeGameFloor": close_game_floor_applied,
            "shotAttempts": safe_round(shot_attempts, 2),
            "pregameProjection": pregame_projection,
            "gamePacePct": pace_pct,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
