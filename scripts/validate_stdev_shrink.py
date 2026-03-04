#!/usr/bin/env python3
"""
Validate per-stat stdev shrinkage factor via backtest sweep.

For each (stat, shrink_value), runs a backtest with STDEV_SHRINK_<STAT>=<value>
and extracts per-stat Brier score + real-line ROI.  Outputs a comparison table
and a recommendation (best Brier, best ROI, consensus).

Follows the same subprocess pattern as validate_shrink_k.py.

Usage:
    .venv/Scripts/python.exe scripts/validate_stdev_shrink.py \
        --date-from 2025-10-21 --date-to 2025-11-30 \
        --stat pts --output data/stdev_shrink_pts.json

    # Sweep multiple stats in sequence:
    .venv/Scripts/python.exe scripts/validate_stdev_shrink.py \
        --date-from 2025-10-21 --date-to 2025-11-30 \
        --stat pts,ast --shrink-values 0.55,0.60,0.65,0.70,0.75,0.80,0.85
"""

import argparse
import json
import os
import sys
import subprocess


def _run_backtest(shrink_stat, shrink_value, date_from, date_to, python_exe):
    """Run a backtest subprocess with STDEV_SHRINK_<STAT>=<value>, return parsed JSON."""
    env = os.environ.copy()
    env_key = f"STDEV_SHRINK_{shrink_stat.upper()}"
    env[env_key] = str(shrink_value)
    cmd = [
        python_exe, "nba_mod.py", "backtest",
        date_from, date_to,
        "--model", "full", "--local",
        "--odds-source", "local_history",
    ]
    print(
        f"[{shrink_stat} shrink={shrink_value:.2f}] Running: {' '.join(cmd)}",
        file=sys.stderr, flush=True,
    )
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    if proc.returncode != 0:
        print(f"[{shrink_stat} shrink={shrink_value:.2f}] STDERR:\n{proc.stderr}",
              file=sys.stderr, flush=True)
        raise RuntimeError(
            f"Backtest for {shrink_stat} shrink={shrink_value} failed (rc={proc.returncode})"
        )

    # Last stdout line is the JSON payload
    lines = proc.stdout.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"No JSON output found for {shrink_stat} shrink={shrink_value}")


def _extract_metrics(result, stat):
    """Extract per-stat Brier, ROI, and sample counts from backtest result."""
    cal_by_stat = result.get("calibrationByStat", {})
    stat_cal = cal_by_stat.get(stat, {})
    brier = stat_cal.get("brier")

    real_stat_roi = result.get("realLineStatRoi", {})
    stat_roi_info = real_stat_roi.get(stat, {})
    roi_real = stat_roi_info.get("roi")
    n_real = stat_roi_info.get("n", 0)

    return {
        "brier": round(brier, 6) if brier is not None else None,
        "roi_real_pct": round(roi_real * 100, 3) if roi_real is not None else None,
        "real_samples": n_real,
        "total_bets": result.get("sampleCount", 0),
        "hit_rate_pct": round(result.get("hitRate", 0) * 100, 3),
    }


def _recommend(sweep_results):
    """Pick best shrink value by Brier, ROI, and consensus."""
    valid = [(sv, m) for sv, m in sweep_results if m["brier"] is not None]
    if not valid:
        return {"best_brier": None, "best_roi": None, "consensus": None}

    best_brier_sv = min(valid, key=lambda x: x[1]["brier"])
    best_roi_sv = max(valid, key=lambda x: x[1]["roi_real_pct"] or -999)

    if best_brier_sv[0] == best_roi_sv[0]:
        consensus = best_brier_sv[0]
    else:
        consensus = None

    return {
        "best_brier": {"shrink": best_brier_sv[0], "brier": best_brier_sv[1]["brier"]},
        "best_roi": {"shrink": best_roi_sv[0], "roi_real_pct": best_roi_sv[1]["roi_real_pct"]},
        "consensus": consensus,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Validate per-stat stdev shrinkage factor via backtest sweep"
    )
    parser.add_argument("--date-from", required=True, help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--date-to", required=True, help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--stat", required=True,
                        help="Stat(s) to sweep, comma-separated (e.g. pts or pts,ast)")
    parser.add_argument("--shrink-values", default="0.55,0.60,0.65,0.70,0.75,0.80,0.85",
                        help="Comma-separated shrink values to test (default: 0.55-0.85 by 0.05)")
    parser.add_argument("--output", default=None,
                        help="Output JSON path (default: data/stdev_shrink_<stat>.json)")
    parser.add_argument("--python-exe", default=None,
                        help="Python executable path (default: auto-detect)")
    args = parser.parse_args()

    stats = [s.strip() for s in args.stat.split(",")]
    shrink_values = [float(v.strip()) for v in args.shrink_values.split(",")]

    # Auto-detect python exe
    python_exe = args.python_exe
    if python_exe is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidate = os.path.join(repo_root, ".venv", "Scripts", "python.exe")
        if os.path.exists(candidate):
            python_exe = candidate
        else:
            python_exe = sys.executable

    all_results = {}

    for stat in stats:
        print(f"\n{'='*60}", file=sys.stderr, flush=True)
        print(f"Sweeping stdev shrink for stat={stat}", file=sys.stderr, flush=True)
        print(f"Values: {shrink_values}", file=sys.stderr, flush=True)
        print(f"{'='*60}", file=sys.stderr, flush=True)

        sweep = []
        for sv in shrink_values:
            result = _run_backtest(stat, sv, args.date_from, args.date_to, python_exe)
            if not result.get("success"):
                print(f"ERROR: Backtest for {stat} shrink={sv} failed: {result.get('error')}",
                      file=sys.stderr)
                sys.exit(1)
            metrics = _extract_metrics(result, stat)
            sweep.append((sv, metrics))
            print(
                f"  shrink={sv:.2f} → Brier={metrics['brier']}  "
                f"ROI_real={metrics['roi_real_pct']}%  n_real={metrics['real_samples']}",
                file=sys.stderr, flush=True,
            )

        recommendation = _recommend(sweep)
        all_results[stat] = {
            "sweep": {str(sv): m for sv, m in sweep},
            "recommendation": recommendation,
        }

        print(f"\n[{stat}] Recommendation: {recommendation}", file=sys.stderr, flush=True)

    output = {
        "config": {
            "date_from": args.date_from,
            "date_to": args.date_to,
            "stats": stats,
            "shrink_values": shrink_values,
            "baseline_shrink": 0.75,
        },
        "results": all_results,
    }

    # Write output
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = args.output
    if out_path is None:
        out_path = os.path.join("data", f"stdev_shrink_{'_'.join(stats)}.json")
    if not os.path.isabs(out_path):
        out_path = os.path.join(repo_root, out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"\nResults written to: {out_path}", file=sys.stderr, flush=True)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
