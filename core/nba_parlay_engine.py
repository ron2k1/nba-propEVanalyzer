#!/usr/bin/env python3
"""Correlation-aware parlay EV math."""

import math
import statistics
from statistics import NormalDist

from .nba_data_collection import safe_round
from .nba_ev_engine import american_to_decimal, prob_to_american

_SAME_PLAYER_CORR = {
    frozenset(["pts", "reb"]): 0.32,
    frozenset(["pts", "ast"]): 0.28,
    frozenset(["pts", "fg3m"]): 0.48,
    frozenset(["pts", "stl"]): 0.18,
    frozenset(["pts", "blk"]): 0.08,
    frozenset(["pts", "tov"]): 0.22,
    frozenset(["pts", "pra"]): 0.90,
    frozenset(["pts", "pr"]): 0.88,
    frozenset(["pts", "pa"]): 0.86,
    frozenset(["reb", "ast"]): 0.15,
    frozenset(["reb", "blk"]): 0.35,
    frozenset(["reb", "pra"]): 0.75,
    frozenset(["reb", "pr"]): 0.72,
    frozenset(["reb", "ra"]): 0.70,
    frozenset(["reb", "stl"]): 0.16,
    frozenset(["ast", "pra"]): 0.70,
    frozenset(["ast", "pa"]): 0.72,
    frozenset(["ast", "ra"]): 0.68,
    frozenset(["ast", "stl"]): 0.22,
    frozenset(["stl", "blk"]): 0.25,
    frozenset(["fg3m", "pra"]): 0.55,
    frozenset(["fg3m", "pr"]): 0.52,
}
_SAME_TEAM_DIFF_PLAYER_CORR = {
    frozenset(["pts", "pts"]): -0.12,
    frozenset(["ast", "ast"]): -0.08,
    frozenset(["reb", "reb"]): -0.05,
    frozenset(["pts", "ast"]): 0.05,
    frozenset(["pts", "fg3m"]): -0.04,
}


def _get_stat_correlation(stat1, stat2, player1_id, player2_id, player1_team, player2_team, logs1=None, logs2=None):
    same_player = player1_id == player2_id
    same_team = (player1_team == player2_team) and not same_player
    pair = frozenset([stat1, stat2])

    if same_player:
        if logs1 and len(logs1) >= 10:
            vals1 = [g.get(stat1, 0) for g in logs1]
            vals2 = [g.get(stat2, 0) for g in logs1]
            n = len(vals1)
            if n >= 5:
                mean1 = statistics.mean(vals1)
                mean2 = statistics.mean(vals2)
                std1 = statistics.stdev(vals1) if n >= 2 else 0
                std2 = statistics.stdev(vals2) if n >= 2 else 0
                if std1 > 0 and std2 > 0:
                    cov = sum((v1 - mean1) * (v2 - mean2) for v1, v2 in zip(vals1, vals2)) / (n - 1)
                    return max(-0.99, min(0.99, cov / (std1 * std2)))
        return _SAME_PLAYER_CORR.get(pair, 0.20)

    if same_team:
        return _SAME_TEAM_DIFF_PLAYER_CORR.get(pair, -0.05)
    return 0.0


def _probit(p):
    return NormalDist().inv_cdf(max(0.0001, min(0.9999, float(p))))


def _phi(z):
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def _joint_prob_2(p1, p2, rho):
    z1 = _probit(p1)
    z2 = _probit(p2)
    joint = p1 * p2 + rho * _phi(z1) * _phi(z2)
    return max(0.0001, min(0.9999, joint))


def _joint_prob_3(p1, p2, p3, r12, r13, r23):
    z1, z2, z3 = _probit(p1), _probit(p2), _probit(p3)
    base = p1 * p2 * p3
    c12 = r12 * _phi(z1) * _phi(z2) * p3
    c13 = r13 * _phi(z1) * _phi(z3) * p2
    c23 = r23 * _phi(z2) * _phi(z3) * p1
    return max(0.0001, min(0.9999, base + c12 + c13 + c23))


def compute_parlay_ev(legs):
    if len(legs) < 2 or len(legs) > 3:
        return {"success": False, "error": "Parlay requires 2 or 3 legs."}

    resolved = []
    for i, leg in enumerate(legs):
        prob_over = float(leg.get("probOver", 0.5))
        side = str(leg.get("side", "over")).lower()
        p_side = prob_over if side == "over" else (1.0 - prob_over)
        odds = int(leg.get("overOdds", -110) if side == "over" else leg.get("underOdds", -110))
        dec_odds = american_to_decimal(odds)
        if dec_odds is None or dec_odds <= 1.0:
            return {"success": False, "error": f"Invalid odds on leg {i + 1}: {odds}"}

        resolved.append(
            {
                "legIndex": i + 1,
                "playerId": int(leg.get("playerId", 0)),
                "playerTeam": str(leg.get("playerTeam", "")),
                "stat": str(leg.get("stat", "")),
                "line": float(leg.get("line", 0)),
                "side": side,
                "pSide": safe_round(p_side, 4),
                "odds": odds,
                "decOdds": dec_odds,
                "gameLogs": leg.get("gameLogs"),
            }
        )

    n = len(resolved)
    corr = [[1.0] * n for _ in range(n)]
    corr_labels = {}
    for i in range(n):
        for j in range(i + 1, n):
            a, b = resolved[i], resolved[j]
            rho = _get_stat_correlation(
                a["stat"],
                b["stat"],
                a["playerId"],
                b["playerId"],
                a["playerTeam"],
                b["playerTeam"],
                a["gameLogs"],
                b["gameLogs"],
            )
            corr[i][j] = rho
            corr[j][i] = rho
            corr_labels[f"leg{i+1}_leg{j+1}"] = safe_round(rho, 3)

    probs = [r["pSide"] for r in resolved]
    joint_prob = _joint_prob_2(probs[0], probs[1], corr[0][1]) if n == 2 else _joint_prob_3(
        probs[0], probs[1], probs[2], corr[0][1], corr[0][2], corr[1][2]
    )

    naive_prob = 1.0
    for r in resolved:
        naive_prob *= r["pSide"]

    parlay_dec = 1.0
    for r in resolved:
        parlay_dec *= r["decOdds"]

    ev_unit = joint_prob * (parlay_dec - 1.0) - (1.0 - joint_prob)
    ev_pct = ev_unit * 100.0
    kelly = max(0.0, ev_unit / (parlay_dec - 1.0)) if ev_unit > 0.0 else 0.0

    if ev_pct <= 0:
        verdict = "Negative EV"
    elif ev_pct < 2:
        verdict = "Thin Edge"
    elif ev_pct < 5:
        verdict = "Good Value"
    else:
        verdict = "Strong Value"

    corr_impact = safe_round((joint_prob - naive_prob) * 100, 2)
    return {
        "success": True,
        "legs": [{k: v for k, v in r.items() if k != "gameLogs"} for r in resolved],
        "correlations": corr_labels,
        "jointProb": safe_round(joint_prob, 4),
        "naiveJointProb": safe_round(naive_prob, 4),
        "correlationImpact": corr_impact,
        "parlayDecOdds": safe_round(parlay_dec, 4),
        "parlayAmericanOdds": prob_to_american(1.0 / parlay_dec) if parlay_dec > 1 else None,
        "ev": safe_round(ev_unit, 4),
        "evPercent": safe_round(ev_pct, 2),
        "kellyFraction": safe_round(kelly, 4),
        "halfKelly": safe_round(kelly / 2, 4),
        "verdict": verdict,
        "legCount": n,
    }
