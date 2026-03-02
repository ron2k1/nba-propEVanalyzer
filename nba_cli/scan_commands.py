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


_COMMANDS = {
    "roster_sweep": _handle_roster_sweep,
}
