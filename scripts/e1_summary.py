from __future__ import annotations

import argparse
import json
from math import sqrt
from pathlib import Path


DEFAULT_REAL_ONLY = Path(
    "data/backtest_results/2025-11-17_to_2026-02-25_full_local_realonly.json"
)
DEFAULT_MATCH_LIVE = Path(
    "data/backtest_results/2025-11-17_to_2026-02-25_full_local_matchlive_noblend_opening.json"
)
GRADED_OUTCOMES = {"win", "loss"}


def _player_name(bet: dict) -> str | None:
    return bet.get("player_name") or bet.get("playerName")


def _bets_from_artifact(obj: dict) -> list[dict]:
    bets = obj.get("bets")
    if isinstance(bets, list):
        return bets
    if isinstance(bets, dict):
        full = bets.get("full")
        if isinstance(full, list):
            return full
    return []


def _wilson_interval(win_count: int, total_count: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total_count <= 0:
        return None, None
    p = win_count / total_count
    denom = 1.0 + (z * z / total_count)
    center = (p + (z * z / (2.0 * total_count))) / denom
    half = (
        z
        * sqrt((p * (1.0 - p) + (z * z / (4.0 * total_count))) / total_count)
        / denom
    )
    return center, half


def _metric_block(bets: list[dict]) -> dict:
    wins = sum(1 for bet in bets if bet.get("outcome") == "win")
    pnl = sum(float(bet.get("pnl") or 0.0) for bet in bets)
    center, half = _wilson_interval(wins, len(bets))
    return {
        "bets": len(bets),
        "wins": wins,
        "losses": len(bets) - wins,
        "hitRatePct": round(100.0 * wins / len(bets), 3) if bets else None,
        "roiPctPerBet": round(100.0 * pnl / len(bets), 3) if bets else None,
        "hitRate95WilsonPct": {
            "low": round(100.0 * (center - half), 3) if center is not None else None,
            "high": round(100.0 * (center + half), 3) if center is not None else None,
        },
    }


def _strict_subset(
    bets: list[dict],
    stats: set[str],
    bins: set[int],
) -> list[dict]:
    return [
        bet
        for bet in bets
        if bet.get("used_real_line")
        and bet.get("policy_pass")
        and bet.get("stat") in stats
        and int(bet.get("bin", -1)) in bins
        and bet.get("outcome") in GRADED_OUTCOMES
    ]


def _summarize_artifact(path: Path, stats: set[str], bins: set[int]) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8"))
    bets = _bets_from_artifact(obj)
    graded = [bet for bet in bets if bet.get("outcome") in GRADED_OUTCOMES]
    strict = _strict_subset(bets, stats, bins)
    reports_full = (obj.get("reports") or {}).get("full") or {}

    by_stat = {
        stat: _metric_block([bet for bet in strict if bet.get("stat") == stat])
        for stat in sorted(stats)
    }
    by_bin = {
        str(bin_idx): _metric_block(
            [bet for bet in strict if int(bet.get("bin", -1)) == bin_idx]
        )
        for bin_idx in sorted(bins)
    }

    return {
        "artifact": str(path),
        "savedTo": obj.get("savedTo"),
        "oddsSource": obj.get("oddsSource"),
        "matchLive": bool(obj.get("matchLive")),
        "lineTiming": obj.get("lineTiming"),
        "walkForward": bool(obj.get("walkForward") or obj.get("walk_forward")),
        "gradedBetsTotal": len(graded),
        "strictSubset": _metric_block(strict),
        "strictByStat": by_stat,
        "strictByBin": by_bin,
        "brierByStat": reports_full.get("brierByStat") or {},
    }


def _bet_identity(bet: dict) -> tuple:
    return (
        bet.get("date"),
        _player_name(bet),
        bet.get("stat"),
        bet.get("side"),
        float(bet.get("line") or 0.0),
        int(bet.get("bin", -1)),
    )


def _compare_overlap(left_path: Path, right_path: Path, stats: set[str], bins: set[int]) -> dict:
    left_obj = json.loads(left_path.read_text(encoding="utf-8"))
    right_obj = json.loads(right_path.read_text(encoding="utf-8"))
    left = _strict_subset(_bets_from_artifact(left_obj), stats, bins)
    right = _strict_subset(_bets_from_artifact(right_obj), stats, bins)
    left_keys = {_bet_identity(bet) for bet in left}
    right_keys = {_bet_identity(bet) for bet in right}
    return {
        "leftOnly": len(left_keys - right_keys),
        "rightOnly": len(right_keys - left_keys),
        "overlap": len(left_keys & right_keys),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize the E1 backtest matrix from saved artifacts."
    )
    parser.add_argument(
        "--real-only",
        default=str(DEFAULT_REAL_ONLY),
        help="Path to the real-only artifact JSON.",
    )
    parser.add_argument(
        "--match-live",
        default=str(DEFAULT_MATCH_LIVE),
        help="Path to the match-live artifact JSON.",
    )
    parser.add_argument(
        "--stats",
        default="pts,ast",
        help="Comma-separated stat whitelist for the strict subset.",
    )
    parser.add_argument(
        "--bins",
        default="0,9",
        help="Comma-separated prob bins for the strict subset.",
    )
    args = parser.parse_args()

    stats = {part.strip() for part in args.stats.split(",") if part.strip()}
    bins = {int(part.strip()) for part in args.bins.split(",") if part.strip()}
    real_only_path = Path(args.real_only)
    match_live_path = Path(args.match_live)

    payload = {
        "strictFilter": {
            "usedRealLine": True,
            "policyPass": True,
            "stats": sorted(stats),
            "bins": sorted(bins),
            "gradedOutcomes": sorted(GRADED_OUTCOMES),
        },
        "artifacts": {
            "realOnly": _summarize_artifact(real_only_path, stats, bins),
            "matchLive": _summarize_artifact(match_live_path, stats, bins),
        },
        "overlap": _compare_overlap(real_only_path, match_live_path, stats, bins),
    }
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
