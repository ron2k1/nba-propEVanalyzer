#!/usr/bin/env python3
"""Scan commands: roster_sweep — scans LineStore snapshots and journals qualifying signals."""

from datetime import datetime, timezone


def _handle_roster_sweep(argv):
    """
    roster_sweep [date]

    For each unique (player, stat) in today's LineStore snapshots:
      1. Calls compute_prop_ev() with the current book line
      2. If _qualifies(): logs to DecisionJournal
    Returns summary: {scanned, logged, skipped, top5}
    """
    from core.nba_line_store import LineStore
    from core.nba_decision_journal import DecisionJournal, _qualifies
    from core.nba_model_training import american_to_implied_prob, compute_prop_ev
    from core.nba_data_collection import safe_round, get_yesterdays_team_abbrs, get_todays_game_totals
    from nba_api.stats.static import players as nba_players_static

    date_str = argv[2] if len(argv) > 2 else datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build game context once before the loop (one scoreboard call + one Odds API call).
    yesterday_teams = get_yesterdays_team_abbrs(date_str)   # free — NBA scoreboard
    game_totals     = get_todays_game_totals(date_str)       # 1 Odds API credit for all games

    ls = LineStore()
    snaps = ls.get_snapshots(date_str)
    if not snaps:
        return {
            "success": True,
            "date": date_str,
            "scanned": 0,
            "logged": 0,
            "skipped": 0,
            "message": "No snapshots found for date",
        }

    # Filter out stale events: late-night games from yesterday bleed into
    # today's UTC-dated JSONL file.  Only keep snapshots whose event matchup
    # appears in today's actual NBA schedule.
    from core.nba_data_collection import get_todays_games
    _today_games = get_todays_games()
    _today_matchups = set()
    for _g in _today_games.get("games", []):
        _h = _g.get("homeTeam", {}).get("abbreviation", "").upper()
        _a = _g.get("awayTeam", {}).get("abbreviation", "").upper()
        if _h and _a:
            _today_matchups.add(frozenset({_h, _a}))
    if _today_matchups:
        snaps = [
            s for s in snaps
            if frozenset({
                s.get("home_team_abbr", "").upper(),
                s.get("away_team_abbr", "").upper(),
            }) in _today_matchups
        ]

    # Deduplicate: keep latest snapshot per (player, stat, book)
    latest = {}
    for s in sorted(snaps, key=lambda x: x.get("timestamp_utc", "")):
        key = (s.get("player_name", ""), s.get("stat", ""), s.get("book", ""))
        latest[key] = s

    # Further deduplicate: per (player, stat), pick the highest-priority book.
    # Pinnacle is tracked separately as a reference (not a betting book) so it
    # can populate referenceBook for the Pinnacle confirmation gate in _qualifies().
    # Priority order: betmgm > draftkings > fanduel > other
    BOOK_PRIO = {"betmgm": 0, "draftkings": 1, "fanduel": 2}
    best_per_player_stat = {}
    pinnacle_per_player_stat = {}
    for (pname, stat, book), snap in latest.items():
        key2 = (pname, stat)
        if book.lower() == "pinnacle":
            pinnacle_per_player_stat[key2] = snap
            continue
        existing = best_per_player_stat.get(key2)
        if existing is None:
            best_per_player_stat[key2] = snap
        else:
            if BOOK_PRIO.get(book, 99) < BOOK_PRIO.get(existing.get("book", ""), 99):
                best_per_player_stat[key2] = snap

    # Build player_name → team_abbr mapping from current rosters of today's
    # playing teams only.  CommonTeamRoster reflects trades immediately,
    # unlike LeagueDashPlayerStats which aggregates across the full season.
    import re as _re

    def _norm_name(n):
        """Normalize player name for matching: strip periods, hyphens, lowercase."""
        return _re.sub(r"[.\-'']", "", str(n)).lower().strip()

    _player_team_map = {}  # normalized player name → uppercase team abbr
    try:
        from nba_api.stats.endpoints import commonteamroster as _ctr
        from nba_api.stats.static import teams as _nba_teams
        from core.nba_data_collection import CURRENT_SEASON, retry_api_call, API_DELAY
        import time as _time

        # Collect unique team abbreviations from today's snapshots
        _playing_abbrs = set()
        for _s in snaps:
            for _k in ("home_team_abbr", "away_team_abbr"):
                _v = (_s.get(_k) or "").upper()
                if _v:
                    _playing_abbrs.add(_v)

        for _abbr in _playing_abbrs:
            _tinfo = _nba_teams.find_team_by_abbreviation(_abbr)
            if not _tinfo:
                continue
            try:
                _time.sleep(API_DELAY)
                _roster = retry_api_call(
                    lambda tid=str(_tinfo["id"]): _ctr.CommonTeamRoster(
                        team_id=tid, season=CURRENT_SEASON, timeout=30,
                    )
                ).get_normalized_dict().get("CommonTeamRoster", [])
                for _row in _roster:
                    _pn = _norm_name(_row.get("PLAYER", ""))
                    if _pn:
                        _player_team_map[_pn] = _abbr
            except Exception:
                continue
    except Exception:
        pass  # fallback: skip enrichment, rely on snapshot fields

    scanned = 0
    logged = 0
    skipped_list = []
    top_results = []

    dj = DecisionJournal()

    for (pname, stat), snap in best_per_player_stat.items():
        scanned += 1
        try:
            # Resolve player ID via exact then partial match
            matches = nba_players_static.find_players_by_full_name(pname)
            if not matches:
                matches = [
                    p for p in nba_players_static.get_players()
                    if pname.lower() in p.get("full_name", "").lower()
                ]
            if not matches:
                skipped_list.append({"player": pname, "stat": stat, "reason": "player_not_found"})
                continue
            player_id = matches[0]["id"]
            player_name = matches[0]["full_name"]

            line = snap.get("line")
            over_odds = snap.get("over_odds", -110)
            under_odds = snap.get("under_odds", -110)
            book = snap.get("book", "")
            team_abbr = snap.get("player_team_abbr", "")
            opp_abbr = snap.get("opponent_abbr", "")
            is_home = snap.get("is_home")

            # Enrich missing team/opponent from player→team map + event context
            if not team_abbr:
                team_abbr = _player_team_map.get(_norm_name(pname), "")
            h = snap.get("home_team_abbr", "").upper()
            a = snap.get("away_team_abbr", "").upper()
            if team_abbr and not opp_abbr:
                if team_abbr.upper() == h:
                    opp_abbr = a
                    is_home = True
                elif team_abbr.upper() == a:
                    opp_abbr = h
                    is_home = False

            # Skip phantom players: team not in this event's matchup
            if team_abbr and h and a and team_abbr.upper() not in (h, a):
                skipped_list.append({"player": pname, "stat": stat, "reason": "team_not_in_event"})
                continue

            if line is None or not opp_abbr:
                skipped_list.append({"player": pname, "stat": stat, "reason": "missing_line_or_opponent"})
                continue

            opp_is_b2b  = opp_abbr.upper() in yesterday_teams
            gtotal      = game_totals.get(frozenset({
                (team_abbr or "").upper(), opp_abbr.upper()
            }))

            result = compute_prop_ev(
                player_id=player_id,
                opponent_abbr=opp_abbr,
                is_home=bool(is_home) if is_home is not None else True,
                stat=stat,
                line=float(line),
                over_odds=int(over_odds or -110),
                under_odds=int(under_odds or -110),
                is_b2b=False,
                player_team_abbr=team_abbr or None,
                opponent_is_b2b=opp_is_b2b,
                game_total=gtotal,
            )

            if not result.get("success"):
                skipped_list.append({
                    "player": pname, "stat": stat,
                    "reason": result.get("error", "ev_failed"),
                })
                continue

            # Inject Pinnacle referenceBook from LineStore snapshots if available.
            # No extra API call needed — Pinnacle data was already fetched by collect_lines.
            # This activates the Pinnacle confirmation gate in _qualifies().
            pinn_snap = pinnacle_per_player_stat.get((pname, stat))
            if pinn_snap and result.get("referenceBook") is None:
                _po = american_to_implied_prob(pinn_snap.get("over_odds"))
                _pu = american_to_implied_prob(pinn_snap.get("under_odds"))
                if _po and _pu and (_po + _pu) > 0:
                    _t = _po + _pu
                    result["referenceBook"] = {
                        "book": "pinnacle",
                        "line": pinn_snap.get("line"),
                        "overOdds": pinn_snap.get("over_odds"),
                        "underOdds": pinn_snap.get("under_odds"),
                        "noVigOver": safe_round(_po / _t, 4),
                        "noVigUnder": safe_round(_pu / _t, 4),
                    }

            qualifies_ok, skip_reason = _qualifies(result, stat, used_real_line=True)
            if not qualifies_ok:
                skipped_list.append({"player": pname, "stat": stat, "reason": skip_reason})
                continue

            ev = result.get("ev") or {}
            eo = float((ev.get("over") or {}).get("edge") or 0.0)
            eu = float((ev.get("under") or {}).get("edge") or 0.0)
            rec = "over" if eo >= eu else "under"
            proj = result.get("projection") or {}

            ctx = {
                "source": "roster_sweep",
                "book": book,
                "snapshotTs": snap.get("timestamp_utc"),
                "oppIsB2B": opp_is_b2b,
            }
            if gtotal is not None:
                ctx["gameTotal"] = gtotal
            ref_book = result.get("referenceBook")
            if ref_book:
                ctx["referenceBook"] = ref_book
            hv = (result.get("projection") or {}).get("recentHighVariance")
            if hv is not None:
                ctx["recentHighVariance"] = hv

            djr = dj.log_signal(
                player_id=player_id, player_name=player_name,
                team_abbr=team_abbr or "", opponent_abbr=opp_abbr,
                stat=stat, line=float(line), book=book,
                over_odds=int(over_odds or -110), under_odds=int(under_odds or -110),
                projection=float(proj.get("projection") or 0.0),
                prob_over=float(ev.get("probOver") or 0.0),
                prob_under=float(ev.get("probUnder") or 0.0),
                edge_over=eo, edge_under=eu, recommended_side=rec,
                recommended_edge=max(eo, eu),
                confidence=max(
                    float(ev.get("probOver") or 0.0),
                    float(ev.get("probUnder") or 0.0),
                ),
                used_real_line=True, action_taken=0,
                context=ctx,
            )
            if djr.get("isDuplicate"):
                skipped_list.append({"player": pname, "stat": stat, "reason": "duplicate"})
            elif djr.get("success"):
                logged += 1
                top_results.append({
                    "player": player_name,
                    "stat": stat,
                    "line": line,
                    "side": rec,
                    "edge": safe_round(max(eo, eu), 4),
                    "probOver": safe_round(float(ev.get("probOver") or 0.0), 4),
                    "book": book,
                })
            else:
                skipped_list.append({
                    "player": pname, "stat": stat,
                    "reason": djr.get("error", "log_failed"),
                })

        except Exception as ex:
            skipped_list.append({"player": pname, "stat": stat, "reason": str(ex)})

    dj.close()

    top5 = sorted(top_results, key=lambda x: -x["edge"])[:5]

    return {
        "success": True,
        "date": date_str,
        "scanned": scanned,
        "logged": logged,
        "skipped": len(skipped_list),
        "top5": top5,
    }


def _handle_top_picks(argv):
    """
    top_picks [limit]

    Show top N policy-qualified picks for today + best 2-leg parlay.
    Default limit is 5.
    """
    from itertools import combinations
    from core.nba_bet_tracking import best_plays_for_date
    from core.nba_parlay_engine import compute_parlay_ev
    from core.nba_data_collection import safe_round

    limit = int(argv[2]) if len(argv) > 2 else 5

    result = best_plays_for_date(limit=max(limit, 20))
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "best_plays_for_date failed")}

    all_offers = result.get("topOffers") or []
    qualified = [r for r in all_offers if r.get("policyQualified")]

    # Load full journal entries for enrichment (probOver, odds, book not in topOffers)
    from core.nba_bet_tracking import _load_journal_entries, _today_local_str
    target = result.get("date") or _today_local_str()
    entries = _load_journal_entries()
    journal_by_key = {}
    for e in entries:
        if str(e.get("pickDate")) != target:
            continue
        key = (e.get("playerId"), str(e.get("stat", "")).lower())
        journal_by_key[key] = e  # latest wins (entries are time-sorted)

    top = qualified[:limit]

    top_picks = []
    for i, r in enumerate(top, 1):
        ev_pct = r.get("recommendedEvPct") or 0.0
        pid = r.get("playerId")
        stat = str(r.get("stat", "")).lower()
        je = journal_by_key.get((pid, stat)) or {}
        top_picks.append({
            "rank": i,
            "playerName": r.get("playerName"),
            "stat": r.get("stat"),
            "line": r.get("line"),
            "side": r.get("recommendedSide"),
            "evPct": safe_round(float(ev_pct), 2),
            "projection": r.get("projection"),
            "odds": r.get("recommendedOdds"),
            "probOver": je.get("probOver"),
            "book": je.get("bestOverBook") or je.get("bestUnderBook") or "",
            "opponentAbbr": r.get("opponentAbbr"),
        })

    # --- Best 2-leg parlay from top picks ---
    best_parlay = None
    if len(qualified) >= 2:
        parlay_candidates = qualified[:min(len(qualified), 8)]  # cap combos
        best_ev = -999
        for a, b in combinations(parlay_candidates, 2):
            # Skip same-player parlays (correlated, most books reject)
            if a.get("playerId") == b.get("playerId"):
                continue
            legs = []
            for pick in (a, b):
                pid = pick.get("playerId")
                stat = str(pick.get("stat", "")).lower()
                je = journal_by_key.get((pid, stat)) or {}
                side = str(pick.get("recommendedSide", "over")).lower()
                legs.append({
                    "probOver": je.get("probOver", 0.5),
                    "side": side,
                    "overOdds": je.get("overOdds", -110),
                    "underOdds": je.get("underOdds", -110),
                    "playerId": pid or 0,
                    "playerTeam": je.get("playerTeamAbbr", ""),
                    "stat": stat,
                    "line": pick.get("line", 0),
                })
            pr = compute_parlay_ev(legs)
            if pr.get("success") and (pr.get("evPercent", -999) > best_ev):
                best_ev = pr["evPercent"]
                best_parlay = {
                    "leg1": {
                        "playerName": a.get("playerName"),
                        "stat": a.get("stat"),
                        "line": a.get("line"),
                        "side": a.get("recommendedSide"),
                    },
                    "leg2": {
                        "playerName": b.get("playerName"),
                        "stat": b.get("stat"),
                        "line": b.get("line"),
                        "side": b.get("recommendedSide"),
                    },
                    "jointProb": pr.get("jointProb"),
                    "parlayOdds": pr.get("parlayAmericanOdds"),
                    "evPercent": pr.get("evPercent"),
                    "correlationImpact": pr.get("correlationImpact"),
                    "verdict": pr.get("verdict"),
                }

    out = {
        "success": True,
        "date": result.get("date"),
        "topPicks": top_picks,
        "bestParlay": best_parlay,
        "message": f"Top {len(top_picks)} picks" + (" + best 2-leg parlay" if best_parlay else ""),
    }

    # Human-readable summary
    print(f"\n=== TOP PICKS  {out['date']} ===")
    for p in top_picks:
        print(f"  #{p['rank']}  {p['playerName']}  {p['stat']} {p['side']} {p['line']}  EV={p['evPct']:.1f}%  proj={p['projection']}  odds={p['odds']}")
    if best_parlay:
        l1, l2 = best_parlay["leg1"], best_parlay["leg2"]
        print(f"\n  PARLAY: {l1['playerName']} {l1['stat']} {l1['side']} {l1['line']}")
        print(f"       + {l2['playerName']} {l2['stat']} {l2['side']} {l2['line']}")
        print(f"       EV={best_parlay['evPercent']:.1f}%  odds={best_parlay['parlayOdds']}  verdict={best_parlay['verdict']}")
    print()

    return out


_COMMANDS = {
    "roster_sweep": _handle_roster_sweep,
    "top_picks": _handle_top_picks,
}
