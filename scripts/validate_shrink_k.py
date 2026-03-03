#!/usr/bin/env python3
"""
Validate shrink-k selection via walk-forward day-count, intersection
comparison, permutation test, and deflated Sharpe ratio.

Usage:
    .venv/Scripts/python.exe scripts/validate_shrink_k.py \
        --date-from 2025-12-28 --date-to 2026-02-25 \
        --k-values 8,12 --permutations 1000 \
        --output data/shrink_k_validation.json
"""

import argparse
import json
import math
import os
import subprocess
import sys
from collections import defaultdict

import numpy as np


def _run_backtest_for_k(k_value, date_from, date_to, python_exe):
    """Run a backtest subprocess with SHRINK_K=<k> and --emit-bets, return parsed JSON."""
    env = os.environ.copy()
    env["SHRINK_K"] = str(k_value)
    cmd = [
        python_exe, "nba_mod.py", "backtest",
        date_from, date_to,
        "--model", "full", "--local",
        "--odds-source", "local_history",
        "--emit-bets",
    ]
    print(f"[k={k_value}] Running: {' '.join(cmd)}", file=sys.stderr, flush=True)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    if proc.returncode != 0:
        print(f"[k={k_value}] STDERR:\n{proc.stderr}", file=sys.stderr, flush=True)
        raise RuntimeError(f"Backtest for k={k_value} failed (rc={proc.returncode})")

    # Last stdout line is the JSON payload
    lines = proc.stdout.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"No JSON output found for k={k_value}")


def _daily_pnl(bets):
    """Group bets by date, return {date: {"pnl": float, "count": int}}."""
    daily = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    for b in bets:
        d = b["date"]
        daily[d]["pnl"] += b["pnl"]
        daily[d]["count"] += 1
    return dict(daily)


def _walk_forward_daycount(bets_a, bets_b):
    """Compare daily ROI between two k values on contested days."""
    daily_a = _daily_pnl(bets_a)
    daily_b = _daily_pnl(bets_b)

    contested_days = sorted(set(daily_a.keys()) & set(daily_b.keys()))
    a_wins, b_wins, ties = 0, 0, 0

    for d in contested_days:
        roi_a = daily_a[d]["pnl"] / daily_a[d]["count"] if daily_a[d]["count"] else 0
        roi_b = daily_b[d]["pnl"] / daily_b[d]["count"] if daily_b[d]["count"] else 0
        if abs(roi_a - roi_b) < 1e-9:
            ties += 1
        elif roi_a > roi_b:
            a_wins += 1
        else:
            b_wins += 1

    n = len(contested_days)
    # Binomial test: H0 is a_wins ~ Binom(n - ties, 0.5)
    effective_n = a_wins + b_wins
    if effective_n > 0:
        from scipy.stats import binomtest
        p_val = binomtest(a_wins, effective_n, 0.5, alternative="greater").pvalue
    else:
        p_val = 1.0

    return {
        "contested_days": n,
        "k8_wins": a_wins,
        "k12_wins": b_wins,
        "ties": ties,
        "k8_win_pct": round(a_wins / effective_n, 4) if effective_n > 0 else None,
        "binomial_p": round(p_val, 6),
    }


def _intersection_comparison(bets_a, bets_b):
    """Compare ROI on bets placed by both k values (matched on player+stat+side+date)."""
    def _key(b):
        return (b["date"], b["player_id"], b["stat"], b["side"])

    idx_a = {_key(b): b for b in bets_a}
    idx_b = {_key(b): b for b in bets_b}

    common_keys = set(idx_a.keys()) & set(idx_b.keys())
    if not common_keys:
        return {
            "intersection_bets": 0,
            "k8_roi_on_intersection": None,
            "k12_roi_on_intersection": None,
        }

    pnl_a = sum(idx_a[k]["pnl"] for k in common_keys)
    pnl_b = sum(idx_b[k]["pnl"] for k in common_keys)
    n = len(common_keys)

    return {
        "intersection_bets": n,
        "k8_roi_on_intersection": round(pnl_a / n, 6) if n else None,
        "k12_roi_on_intersection": round(pnl_b / n, 6) if n else None,
    }


def _permutation_test(bets_a, bets_b, n_permutations=1000, rng_seed=42):
    """
    Within-day permutation test for ROI difference.

    For each day, randomly reassign actual outcomes between k=8's and k=12's
    bet selections (preserving day structure). Compute ROI difference under
    each permutation. p-value = fraction of permuted diffs >= observed diff.
    """
    rng = np.random.default_rng(rng_seed)

    # Group bets by date
    daily_a = defaultdict(list)
    daily_b = defaultdict(list)
    for b in bets_a:
        daily_a[b["date"]].append(b)
    for b in bets_b:
        daily_b[b["date"]].append(b)

    all_dates = sorted(set(daily_a.keys()) | set(daily_b.keys()))

    # Observed ROI difference
    total_pnl_a = sum(b["pnl"] for b in bets_a)
    total_pnl_b = sum(b["pnl"] for b in bets_b)
    n_a = len(bets_a) or 1
    n_b = len(bets_b) or 1
    observed_diff = (total_pnl_a / n_a) - (total_pnl_b / n_b)

    # Build per-day outcome pools
    permuted_diffs = []
    for _ in range(n_permutations):
        perm_pnl_a, perm_pnl_b = 0.0, 0.0
        perm_n_a, perm_n_b = 0, 0

        for d in all_dates:
            da = daily_a.get(d, [])
            db = daily_b.get(d, [])
            # Pool all outcomes for this day
            all_pnls = [b["pnl"] for b in da] + [b["pnl"] for b in db]
            if not all_pnls:
                continue
            rng.shuffle(all_pnls)
            # Assign first len(da) to "a", rest to "b"
            for p in all_pnls[:len(da)]:
                perm_pnl_a += p
                perm_n_a += 1
            for p in all_pnls[len(da):]:
                perm_pnl_b += p
                perm_n_b += 1

        if perm_n_a > 0 and perm_n_b > 0:
            permuted_diffs.append((perm_pnl_a / perm_n_a) - (perm_pnl_b / perm_n_b))

    if not permuted_diffs:
        return {
            "observed_roi_diff": round(observed_diff, 6),
            "p_value": 1.0,
            "n_permutations": n_permutations,
        }

    p_value = sum(1 for d in permuted_diffs if d >= observed_diff) / len(permuted_diffs)

    return {
        "observed_roi_diff": round(observed_diff, 6),
        "p_value": round(p_value, 6),
        "n_permutations": n_permutations,
    }


def _deflated_sharpe(bets, n_trials=2):
    """
    Compute annualized Sharpe and deflated Sharpe for daily returns.

    Deflated Sharpe adjusts for the number of k values tested (trials).
    """
    daily = _daily_pnl(bets)
    if len(daily) < 2:
        return {
            "sharpe_ratio": None,
            "deflated_sharpe": None,
            "n_trials": n_trials,
        }

    # Daily ROI (pnl / count) for each day
    daily_returns = [v["pnl"] / v["count"] for v in daily.values() if v["count"] > 0]

    if len(daily_returns) < 2:
        return {
            "sharpe_ratio": None,
            "deflated_sharpe": None,
            "n_trials": n_trials,
        }

    mean_r = np.mean(daily_returns)
    std_r = np.std(daily_returns, ddof=1)
    if std_r < 1e-12:
        return {
            "sharpe_ratio": None,
            "deflated_sharpe": None,
            "n_trials": n_trials,
        }

    sharpe = float(mean_r / std_r * math.sqrt(252))

    # Deflated Sharpe: SR* = SR - sqrt(2 * log(n_trials) / T)
    # where T = number of daily observations
    t = len(daily_returns)
    deflation = math.sqrt(2.0 * math.log(max(n_trials, 1)) / max(t, 1))
    deflated = sharpe - deflation

    return {
        "sharpe_ratio": round(sharpe, 4),
        "deflated_sharpe": round(deflated, 4),
        "n_trials": n_trials,
    }


def main():
    parser = argparse.ArgumentParser(description="Validate shrink-k selection")
    parser.add_argument("--date-from", required=True, help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--date-to", required=True, help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--k-values", default="8,12",
                        help="Comma-separated k values to compare (default: 8,12)")
    parser.add_argument("--permutations", type=int, default=1000,
                        help="Number of permutations for permutation test (default: 1000)")
    parser.add_argument("--output", default="data/shrink_k_validation.json",
                        help="Output JSON path (default: data/shrink_k_validation.json)")
    parser.add_argument("--python-exe", default=None,
                        help="Python executable path (default: auto-detect)")
    args = parser.parse_args()

    k_values = [int(k.strip()) for k in args.k_values.split(",")]
    if len(k_values) < 2:
        print("ERROR: Need at least 2 k values to compare", file=sys.stderr)
        sys.exit(1)

    # Auto-detect python exe
    python_exe = args.python_exe
    if python_exe is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidate = os.path.join(repo_root, ".venv", "Scripts", "python.exe")
        if os.path.exists(candidate):
            python_exe = candidate
        else:
            python_exe = sys.executable

    # Run backtests for each k value
    results_by_k = {}
    for k in k_values:
        print(f"\n{'='*60}", file=sys.stderr, flush=True)
        print(f"Running backtest with SHRINK_K={k}", file=sys.stderr, flush=True)
        print(f"{'='*60}", file=sys.stderr, flush=True)
        result = _run_backtest_for_k(k, args.date_from, args.date_to, python_exe)
        if not result.get("success"):
            print(f"ERROR: Backtest for k={k} failed: {result.get('error')}", file=sys.stderr)
            sys.exit(1)
        results_by_k[k] = result

    # Extract bet records
    bets_by_k = {}
    for k, res in results_by_k.items():
        bets = res.get("bets", [])
        if not bets:
            print(f"WARNING: No bets emitted for k={k}", file=sys.stderr)
        bets_by_k[k] = bets
        print(f"[k={k}] {len(bets)} bets emitted", file=sys.stderr, flush=True)

    # Compare first two k values (primary comparison)
    k_a, k_b = k_values[0], k_values[1]
    bets_a, bets_b = bets_by_k[k_a], bets_by_k[k_b]

    print(f"\nComparing k={k_a} vs k={k_b}...", file=sys.stderr, flush=True)

    walk_forward = _walk_forward_daycount(bets_a, bets_b)
    intersection = _intersection_comparison(bets_a, bets_b)
    permutation = _permutation_test(bets_a, bets_b, n_permutations=args.permutations)
    sharpe = _deflated_sharpe(bets_a, n_trials=len(k_values))

    # Decision
    wf_pass = (walk_forward["k8_win_pct"] or 0) > 0.50
    perm_pass = permutation["p_value"] < 0.20

    # Decision logic: inconclusive results preserve the prior (k_a) rather than
    # overturning it. Only revert when k_b outperforms on point estimate AND
    # the permutation test provides supporting evidence (p < 0.30).
    roi_a = sum(b["pnl"] for b in bets_a) / len(bets_a) if bets_a else 0
    roi_b = sum(b["pnl"] for b in bets_b) / len(bets_b) if bets_b else 0
    k_b_outperforms = roi_b > roi_a

    if wf_pass and perm_pass:
        recommendation = f"keep_k{k_a}"
        reasoning = (
            f"k={k_a} wins {walk_forward['k8_win_pct']*100:.1f}% of contested days "
            f"(p={walk_forward['binomial_p']:.4f}), "
            f"permutation p={permutation['p_value']:.4f} < 0.20"
        )
    elif k_b_outperforms and permutation["p_value"] < 0.30:
        recommendation = f"revert_to_k{k_b}"
        reasoning = (
            f"k={k_b} outperforms on point estimate "
            f"(ROI {roi_b*100:.1f}% vs {roi_a*100:.1f}%) "
            f"with permutation p={permutation['p_value']:.4f} < 0.30"
        )
    elif not k_b_outperforms:
        recommendation = f"keep_k{k_a}"
        reasoning = (
            f"k={k_a} leads on point estimate "
            f"(ROI {roi_a*100:.1f}% vs {roi_b*100:.1f}%), "
            f"prior selection retained — equivalent within measurement precision"
        )
    else:
        recommendation = "inconclusive"
        reasoning = (
            f"k={k_b} leads on point estimate but permutation "
            f"p={permutation['p_value']:.4f} >= 0.30 — insufficient evidence to overturn prior"
        )

    output = {
        "config": {
            "date_from": args.date_from,
            "date_to": args.date_to,
            "k_values": k_values,
            "permutations": args.permutations,
        },
        "summary": {
            k: {
                "total_bets": len(bets_by_k[k]),
                "total_pnl": round(sum(b["pnl"] for b in bets_by_k[k]), 4),
                "roi_pct": round(
                    sum(b["pnl"] for b in bets_by_k[k]) / len(bets_by_k[k]) * 100, 3
                ) if bets_by_k[k] else None,
                "hit_rate": round(
                    sum(1 for b in bets_by_k[k] if b["outcome"] == "win") / len(bets_by_k[k]) * 100, 3
                ) if bets_by_k[k] else None,
            }
            for k in k_values
        },
        "walk_forward": walk_forward,
        "intersection_comparison": intersection,
        "permutation_test": permutation,
        "deflated_sharpe": sharpe,
        "decision": {
            "walk_forward_pass": wf_pass,
            "permutation_pass": perm_pass,
            "recommendation": recommendation,
            "reasoning": reasoning,
        },
    }

    # Write output
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = args.output
    if not os.path.isabs(out_path):
        out_path = os.path.join(repo_root, out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults written to: {out_path}", file=sys.stderr, flush=True)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
