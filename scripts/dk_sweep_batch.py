#!/usr/bin/env python3
"""Batch DK sweep: read dk_sweep_input.json, run projections + EV, output results."""

import json
import os
import sys
import time
import traceback

# Force unbuffered output so we can monitor progress
os.environ["PYTHONUNBUFFERED"] = "1"

# Add repo root to path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.nba_data_collection import resolve_player_identifier, safe_round
from core.nba_prep_projection import compute_projection
from core.nba_ev_engine import compute_ev


def _build_player_team_map():
    """One API call to get player_id -> team_abbr for all active NBA players."""
    from nba_api.stats.endpoints import playerindex
    pi = playerindex.PlayerIndex(season="2025-26")
    time.sleep(0.7)
    rows = pi.get_normalized_dict().get("PlayerIndex", [])
    mapping = {}
    for r in rows:
        pid = r.get("PERSON_ID")
        abbr = r.get("TEAM_ABBREVIATION")
        name = f"{r.get('PLAYER_FIRST_NAME', '')} {r.get('PLAYER_LAST_NAME', '')}".strip()
        if pid and abbr:
            mapping[int(pid)] = abbr
            # Also map by normalized name for fallback
            mapping[name.lower()] = {"id": int(pid), "abbr": abbr}
    return mapping


def _resolve_player(name, player_team_map):
    """Resolve player name to (player_id, team_abbr) or (None, None)."""
    # Try the name-based lookup from our team map first
    key = name.strip().lower()
    if key in player_team_map:
        info = player_team_map[key]
        return info["id"], info["abbr"]

    # Fall back to the standard resolver
    resolved = resolve_player_identifier(name)
    if not resolved.get("success"):
        return None, None
    pid = int(resolved["playerId"])
    abbr = player_team_map.get(pid)
    return pid, abbr


def run_batch():
    input_path = os.path.join(_ROOT, "data", "dk_sweep_input.json")
    output_path = os.path.join(_ROOT, "data", "dk_sweep_projections.json")

    with open(input_path) as f:
        entries = json.load(f)

    print(f"[dk_sweep_batch] Loaded {len(entries)} entries from dk_sweep_input.json")
    print("[dk_sweep_batch] Fetching player-team mapping...")
    player_team_map = _build_player_team_map()
    print(f"[dk_sweep_batch] Got {sum(1 for v in player_team_map.values() if isinstance(v, str))} player-team mappings")

    # Pre-resolve all unique players
    unique_players = sorted(set(e["player_name"] for e in entries))
    print(f"[dk_sweep_batch] Resolving {len(unique_players)} unique players...")

    player_cache = {}  # name -> (player_id, team_abbr)
    resolve_failures = []
    for name in unique_players:
        pid, abbr = _resolve_player(name, player_team_map)
        if pid:
            player_cache[name] = (pid, abbr)
        else:
            resolve_failures.append(name)

    print(f"[dk_sweep_batch] Resolved {len(player_cache)}/{len(unique_players)} players")
    if resolve_failures:
        print(f"[dk_sweep_batch] FAILED to resolve: {resolve_failures}")

    # Group entries by (player_id, opponent) to share projections
    # Each compute_projection call is expensive (API calls), so cache by player+opponent
    proj_cache = {}  # (player_id, opponent_abbr, is_home) -> proj_data
    results = []
    errors = []
    skipped = 0

    for i, entry in enumerate(entries):
        name = entry["player_name"]
        stat = entry["stat"]
        line = float(entry["line"])
        over_odds = int(entry["over_odds"])
        under_odds = int(entry["under_odds"])
        home_abbr = entry["home_abbr"]
        away_abbr = entry["away_abbr"]

        if name not in player_cache:
            skipped += 1
            continue

        pid, team_abbr = player_cache[name]

        # Determine is_home and opponent
        if team_abbr == home_abbr:
            is_home = True
            opponent_abbr = away_abbr
        elif team_abbr == away_abbr:
            is_home = False
            opponent_abbr = home_abbr
        else:
            # Team doesn't match either side — skip
            errors.append({"player": name, "error": f"team {team_abbr} not in game {away_abbr}@{home_abbr}"})
            continue

        # Get projection (cached)
        proj_key = (pid, opponent_abbr, is_home)
        if proj_key not in proj_cache:
            try:
                print(f"  [{i+1}/{len(entries)}] Projecting {name} vs {opponent_abbr} ({'H' if is_home else 'A'})...")
                proj_data = compute_projection(
                    player_id=pid,
                    opponent_abbr=opponent_abbr,
                    is_home=is_home,
                    is_b2b=False,
                    blend_with_line={stat: line},
                    model_variant="full",
                )
                proj_cache[proj_key] = proj_data
                time.sleep(0.3)  # Be nice to the API
            except Exception as ex:
                proj_cache[proj_key] = {"success": False, "error": str(ex)}
                errors.append({"player": name, "error": str(ex)})
                traceback.print_exc()
        else:
            proj_data = proj_cache[proj_key]

        if not proj_data.get("success"):
            errors.append({"player": name, "stat": stat, "error": proj_data.get("error", "projection failed")})
            continue

        # Get stat projection
        projections = proj_data.get("projections") or {}
        stat_proj = projections.get(stat)
        if not stat_proj:
            errors.append({"player": name, "stat": stat, "error": f"no projection for stat '{stat}'"})
            continue

        projection_val = float(stat_proj.get("projection", 0))
        stdev_val = float(stat_proj.get("projStdev") or stat_proj.get("stdev") or max(projection_val * 0.2, 1.0))

        # Compute EV
        try:
            ev_data = compute_ev(
                projection_val, line, over_odds, under_odds, stdev_val,
                stat=stat,
            )
        except Exception as ex:
            errors.append({"player": name, "stat": stat, "error": f"compute_ev: {ex}"})
            continue

        if not ev_data:
            errors.append({"player": name, "stat": stat, "error": "compute_ev returned None"})
            continue

        # Extract results
        ev_over = ev_data.get("over") or {}
        ev_under = ev_data.get("under") or {}
        ev_over_pct = float(ev_over.get("edge") or 0)
        ev_under_pct = float(ev_under.get("edge") or 0)
        prob_over = float(ev_data.get("probOver") or 0)
        prob_under = float(ev_data.get("probUnder") or 0)

        if ev_over_pct >= ev_under_pct:
            best_side = "over"
            best_ev_pct = ev_over_pct
        else:
            best_side = "under"
            best_ev_pct = ev_under_pct

        confidence_bin = min(9, max(0, int(prob_over * 10)))

        result = {
            "player_name": name,
            "stat": stat,
            "line": line,
            "over_odds": over_odds,
            "under_odds": under_odds,
            "projection": safe_round(projection_val, 2),
            "stdev": safe_round(stdev_val, 2),
            "probOver": safe_round(prob_over, 4),
            "probUnder": safe_round(prob_under, 4),
            "ev_over_pct": safe_round(ev_over_pct, 4),
            "ev_under_pct": safe_round(ev_under_pct, 4),
            "best_side": best_side,
            "best_ev_pct": safe_round(best_ev_pct, 4),
            "confidence_bin": confidence_bin,
            "home_abbr": home_abbr,
            "away_abbr": away_abbr,
            "player_team_abbr": team_abbr,
            "opponent_abbr": opponent_abbr,
            "is_home": is_home,
            "distributionMode": ev_data.get("distributionMode", "normal"),
            "player_id": pid,
        }
        results.append(result)

        if (i + 1) % 20 == 0:
            print(f"  [{i+1}/{len(entries)}] {len(results)} results so far...")

    # Save output
    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_input": len(entries),
        "total_projected": len(results),
        "total_errors": len(errors),
        "skipped_unresolved": skipped,
        "results": results,
        "errors": errors[:50],  # Cap error output
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n[dk_sweep_batch] DONE: {len(results)} projections, {len(errors)} errors, {skipped} skipped")
    print(f"[dk_sweep_batch] Output: {output_path}")

    # Print summary stats
    if results:
        positive_ev = [r for r in results if r["best_ev_pct"] > 0]
        strong_ev = [r for r in results if r["best_ev_pct"] >= 0.07]
        print(f"  Positive EV: {len(positive_ev)}/{len(results)}")
        print(f"  Strong EV (>=7%): {len(strong_ev)}/{len(results)}")

    return output


if __name__ == "__main__":
    run_batch()
