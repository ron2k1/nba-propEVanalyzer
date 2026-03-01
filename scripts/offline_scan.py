"""Offline EV scanner: uses cached LineStore snapshots, zero API calls to Odds API."""
import json, sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.nba_line_store import LineStore
from core.nba_data_prep import compute_projection
from core.nba_ev_engine import compute_ev
from nba_api.stats.static import players as nba_players_static, teams as nba_teams_static
from core.nba_data_collection import get_team_roster_status

def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-02-27"
    min_edge = float(sys.argv[2]) if len(sys.argv) > 2 else 0.03

    store = LineStore()
    snaps = store.get_snapshots(date_str)
    if not snaps:
        print(json.dumps({"success": False, "error": f"No snapshots for {date_str}"}))
        return

    all_teams = {t["abbreviation"]: t for t in nba_teams_static.get_teams()}

    games = {}
    for s in snaps:
        gid = s.get("game_id", "")
        if gid and gid not in games:
            games[gid] = {"home": s.get("home_team_abbr", ""), "away": s.get("away_team_abbr", "")}

    print(f"Games today ({date_str}): {len(games)}", file=sys.stderr, flush=True)
    for g in games.values():
        print(f"  {g['away']} @ {g['home']}", file=sys.stderr, flush=True)

    team_abbrs = set()
    for g in games.values():
        team_abbrs.add(g["home"])
        team_abbrs.add(g["away"])

    player_to_team = {}
    for abbr in sorted(team_abbrs):
        try:
            result = get_team_roster_status(abbr)
            if result and result.get("success"):
                for r in result.get("players", []):
                    pname = r.get("name", "") or r.get("player", "")
                    if pname:
                        player_to_team[pname.lower()] = abbr
            time.sleep(0.6)
        except Exception as e:
            print(f"  Roster fail for {abbr}: {e}", file=sys.stderr, flush=True)

    print(f"Mapped {len(player_to_team)} players to teams", file=sys.stderr, flush=True)

    # Dedupe: keep latest snap per (player, stat, book)
    latest = {}
    for s in sorted(snaps, key=lambda x: x.get("timestamp_utc", "")):
        key = (s.get("player_name", ""), s.get("stat", ""), s.get("book", ""))
        latest[key] = s

    # Per (player, stat): keep best book by over_odds
    best_by_ps = {}
    for (pname, stat, book), s in latest.items():
        ps_key = (pname, stat)
        if ps_key not in best_by_ps:
            best_by_ps[ps_key] = s
        else:
            existing = best_by_ps[ps_key]
            if (s.get("over_odds") or -999) > (existing.get("over_odds") or -999):
                best_by_ps[ps_key] = s

    unique_snaps = list(best_by_ps.values())
    print(f"Evaluating {len(unique_snaps)} unique player-stat lines...", file=sys.stderr, flush=True)

    all_active = nba_players_static.get_active_players()
    results = []
    errors = 0
    skip_reasons = {}

    def skip(reason):
        nonlocal errors
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        errors += 1

    for i, s in enumerate(unique_snaps):
        player_name = s.get("player_name", "")
        stat = s.get("stat", "")
        line = s.get("line")
        over_odds = s.get("over_odds")
        under_odds = s.get("under_odds")
        book = s.get("book", "")
        home_abbr = s.get("home_team_abbr", "")
        away_abbr = s.get("away_team_abbr", "")

        team_abbr = player_to_team.get(player_name.lower(), "")
        if not team_abbr:
            for mapped_name, mapped_abbr in player_to_team.items():
                if player_name.lower() in mapped_name or mapped_name in player_name.lower():
                    team_abbr = mapped_abbr
                    break
        if not team_abbr:
            skip("no_team")
            continue

        if team_abbr == home_abbr:
            opponent = away_abbr
            is_home = True
        elif team_abbr == away_abbr:
            opponent = home_abbr
            is_home = False
        else:
            skip("team_mismatch")
            continue

        matches = [p for p in all_active if p.get("full_name", "").lower() == player_name.lower()]
        if not matches:
            parts = player_name.lower().split()
            if len(parts) >= 2:
                matches = [p for p in all_active
                           if p.get("last_name", "").lower() == parts[-1]
                           and p.get("first_name", "").lower().startswith(parts[0][:3])]
        if not matches:
            skip("no_player_id")
            continue

        player_id = matches[0]["id"]

        try:
            proj = compute_projection(
                player_id=player_id,
                opponent_abbr=opponent,
                is_home=is_home,
                is_b2b=False,
            )
            if not proj.get("success"):
                skip("proj_fail")
                continue

            projections = proj.get("projections", {})
            stat_proj = projections.get(stat)
            if not stat_proj:
                skip("no_stat_proj")
                continue

            projected = float(stat_proj.get("projection", 0))
            stdev = float(stat_proj.get("projStdev", 0) or stat_proj.get("stdev", 0) or 0)

            ev = compute_ev(projected, line, over_odds, under_odds, stdev=stdev, stat=stat)
            if not ev:
                skip("ev_fail")
                continue

            over_side = ev.get("over", {})
            under_side = ev.get("under", {})
            over_ev = float(over_side.get("evPercent", -99) or -99)
            under_ev = float(under_side.get("evPercent", -99) or -99)

            if over_ev >= under_ev:
                best_side = "over"
                best_ev = over_ev
                best_edge = float(over_side.get("edge", 0) or 0)
                best_prob = float(ev.get("probOver", 0.5) or 0.5)
                best_odds = over_odds
            else:
                best_side = "under"
                best_ev = under_ev
                best_edge = float(under_side.get("edge", 0) or 0)
                best_prob = 1.0 - float(ev.get("probOver", 0.5) or 0.5)
                best_odds = under_odds

            results.append({
                "player": player_name,
                "stat": stat,
                "line": line,
                "side": best_side,
                "projection": round(projected, 1),
                "probWin": round(best_prob, 3),
                "edge": round(best_edge, 3),
                "evPct": round(best_ev, 2),
                "odds": best_odds,
                "book": book,
                "team": team_abbr,
                "opponent": opponent,
            })
        except Exception:
            skip("exception")
            continue

        if (i + 1) % 30 == 0:
            print(f"  [{i+1}/{len(unique_snaps)}] evaluated, {len(results)} with edge so far",
                  file=sys.stderr, flush=True)

    strong = sorted([r for r in results if r["edge"] >= 0.05], key=lambda x: -x["edge"])
    good = sorted([r for r in results if min_edge <= r["edge"] < 0.05], key=lambda x: -x["edge"])
    thin = sorted([r for r in results if 0 < r["edge"] < min_edge], key=lambda x: -x["edge"])

    print(f"Done. {len(strong)} Strong (>=5%), {len(good)} Good (3-5%), {len(thin)} Thin (<3%), "
          f"{errors} skipped.", file=sys.stderr, flush=True)
    print(f"Skip reasons: {json.dumps(skip_reasons)}", file=sys.stderr, flush=True)

    print(json.dumps({
        "success": True,
        "date": date_str,
        "gamesFound": len(games),
        "snapshotsTotal": len(snaps),
        "uniqueLinesEvaluated": len(unique_snaps) - errors,
        "strongValueCount": len(strong),
        "goodValueCount": len(good),
        "skipped": errors,
        "skipReasons": skip_reasons,
        "strongValue": strong[:15],
        "goodValue": good[:10],
    }, indent=2))


if __name__ == "__main__":
    main()
