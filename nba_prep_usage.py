#!/usr/bin/env python3
"""Usage-adjustment prep logic."""

import traceback

from nba_data_collection import CURRENT_SEASON, get_team_roster_status, safe_round

_USG_STAT_ELASTICITY = {
    "pts": 0.80,
    "ast": 0.55,
    "fg3m": 0.65,
    "tov": 0.72,
    "reb": 0.12,
    "stl": 0.18,
    "blk": 0.10,
    "pra": 0.65,
    "pr": 0.55,
    "pa": 0.65,
    "ra": 0.30,
}
_USG_REDISTRIBUTION_DAMPENING = 0.60


def compute_usage_adjustment(player_id, team_abbr, season=None):
    if season is None:
        season = CURRENT_SEASON

    try:
        roster_data = get_team_roster_status(team_abbr, season)
        if not roster_data["success"]:
            return {"success": False, "error": roster_data.get("error", "Roster fetch failed")}

        players = roster_data["players"]
        target = next((p for p in players if p["playerId"] == player_id), None)
        if not target:
            return {
                "success": False,
                "error": f"Player {player_id} not found on {team_abbr} roster",
                "statMultipliers": {s: 1.0 for s in _USG_STAT_ELASTICITY},
            }

        target_usg = target["usgPct"] or 0.0
        absent = [
            p
            for p in players
            if p["playerId"] != player_id
            and p["status"] in ("Likely Inactive", "Inactive")
            and p["usgPct"] >= 18.0
            and p["seasonGP"] >= 10
        ]

        if not absent:
            return {
                "success": True,
                "teamAbbr": team_abbr,
                "playerId": player_id,
                "targetUsgPct": target_usg,
                "estimatedNewUsgPct": target_usg,
                "usageMultiplier": 1.0,
                "effectiveMultiplier": 1.0,
                "statMultipliers": {s: 1.0 for s in _USG_STAT_ELASTICITY},
                "absentTeammates": [],
                "note": "No high-usage teammates flagged as inactive.",
            }

        active_players = [p for p in players if p["status"] == "Active"]
        total_active_usg = sum(p["usgPct"] for p in active_players) or 100.0
        absent_usg_total = sum(p["usgPct"] for p in absent)
        remaining_usg = total_active_usg - (target_usg if target in active_players else 0)
        absent_available = absent_usg_total

        if remaining_usg <= 0 or target_usg <= 0:
            usage_ratio = 1.0
        else:
            target_share_of_remaining = target_usg / max(remaining_usg, 1.0)
            absorbed_usg = absent_available * target_share_of_remaining
            new_usg = target_usg + absorbed_usg
            usage_ratio = new_usg / target_usg

        effective_mult = 1.0 + (usage_ratio - 1.0) * _USG_REDISTRIBUTION_DAMPENING
        effective_mult = max(1.0, min(1.45, effective_mult))

        stat_mults = {stat: safe_round(effective_mult ** elasticity, 3) for stat, elasticity in _USG_STAT_ELASTICITY.items()}
        new_usg_est = safe_round(target_usg * effective_mult, 1)

        return {
            "success": True,
            "teamAbbr": team_abbr,
            "playerId": player_id,
            "targetUsgPct": safe_round(target_usg, 1),
            "estimatedNewUsgPct": new_usg_est,
            "usageMultiplier": safe_round(usage_ratio, 3),
            "effectiveMultiplier": safe_round(effective_mult, 3),
            "statMultipliers": stat_mults,
            "absentTeammates": [
                {"name": p["name"], "usgPct": p["usgPct"], "status": p["status"], "riskLevel": p["riskLevel"]}
                for p in absent
            ],
        }
    except Exception as e:
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}
