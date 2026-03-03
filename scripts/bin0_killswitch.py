#!/usr/bin/env python3
"""
Bin-0 Kill Switch Analysis — Walk-Forward Backtest + Bootstrap CI.

Runs a walk-forward backtest over a date range, filters to bin-0 (0-10%
probability) bets only, and computes bootstrap 95% CI on ROI.

Kill switch verdict:
  - PROCEED: bin-0 CI entirely above zero
  - CAUTIOUS: bin-0 ROI positive but CI includes zero
  - STOP: bin-0 ROI negative or CI entirely below zero

Usage:
    python scripts/bin0_killswitch.py \
        --date-from 2025-10-21 --date-to 2026-02-25 \
        [--walk-forward] [--odds-source local_history] \
        [--n-bootstrap 10000] [--ci-level 0.95] \
        [--output docs/bin0_walkforward_report.md]
"""

import argparse
import json
import math
import os
import random
import sys
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.nba_backtest import run_backtest


def _pnl_for_american(outcome, odds):
    """Compute PnL for a $1 bet at American odds."""
    if outcome == "push":
        return 0.0
    if outcome == "loss":
        return -1.0
    o = float(odds)
    return o / 100.0 if o > 0 else 100.0 / (-o)


def _bootstrap_ci(pnl_list, n_bootstrap=10000, ci_level=0.95):
    """Bootstrap 95% CI on mean ROI from a list of per-bet PnL values."""
    if not pnl_list:
        return None, None, None
    n = len(pnl_list)
    rng = random.Random(42)  # reproducible
    means = []
    for _ in range(n_bootstrap):
        sample = [pnl_list[rng.randint(0, n - 1)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    alpha = 1.0 - ci_level
    lo_idx = max(0, int(math.floor(alpha / 2 * n_bootstrap)))
    hi_idx = min(n_bootstrap - 1, int(math.ceil((1.0 - alpha / 2) * n_bootstrap)))
    mean_roi = sum(pnl_list) / n
    return mean_roi, means[lo_idx], means[hi_idx]


def _extract_bin0_bets(backtest_result):
    """
    Extract bin-0 bet details from a backtest result.

    Unfortunately the standard backtest only reports aggregated bins, not per-bet
    detail. We need to look at the realLineCalibBins for bin 0-10%.

    Returns dict with: bets_placed, wins, pnl, hit_rate, roi
    """
    reports = backtest_result.get("reports", {})
    report = reports.get("full") or reports.get("simple") or {}

    # Overall ROI simulation
    roi_sim = report.get("roiSimulation", {})

    # Real-line bin-0 data
    real_bins = report.get("realLineCalibBins", [])
    if real_bins and len(real_bins) > 0:
        bin0 = real_bins[0]  # 0-10% bin
        return {
            "source": "realLineCalibBins",
            "bets_placed": bin0.get("betsPlaced", bin0.get("count", 0)),
            "wins": bin0.get("wins", 0),
            "pnl": bin0.get("pnlUnits", 0.0),
            "hit_rate": bin0.get("hitRatePct"),
            "roi": bin0.get("roiPctPerBet"),
        }

    # Fallback to overall simulation data
    return {
        "source": "roiSimulation_overall",
        "bets_placed": roi_sim.get("betsPlaced", 0),
        "wins": roi_sim.get("wins", 0),
        "pnl": roi_sim.get("pnlUnits", 0.0),
        "hit_rate": roi_sim.get("hitRatePct"),
        "roi": roi_sim.get("roiPctPerBet"),
    }


def main():
    parser = argparse.ArgumentParser(description="Bin-0 Kill Switch Analysis")
    parser.add_argument("--date-from", default="2025-10-21")
    parser.add_argument("--date-to", default="2026-02-25")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Use date-specific calibration + policy")
    parser.add_argument("--odds-source", default=None,
                        help="'local_history' for real closing lines")
    parser.add_argument("--n-bootstrap", type=int, default=10000)
    parser.add_argument("--ci-level", type=float, default=0.95)
    parser.add_argument("--output", default=os.path.join(ROOT, "docs", "bin0_walkforward_report.md"))
    args = parser.parse_args()

    print(f"=== Bin-0 Kill Switch Analysis ===")
    print(f"  Date range: {args.date_from} -> {args.date_to}")
    print(f"  Walk-forward: {args.walk_forward}")
    print(f"  Odds source: {args.odds_source or 'synthetic'}")
    print(f"  Bootstrap: {args.n_bootstrap} resamples, {args.ci_level:.0%} CI")
    print()

    # Run the backtest
    print("Running backtest...", flush=True)
    result = run_backtest(
        date_from=args.date_from,
        date_to=args.date_to,
        model="full",
        save_results=True,
        fast=True,
        data_source="local",
        odds_source=args.odds_source,
        walk_forward=args.walk_forward,
    )

    if not result.get("success"):
        print(f"ERROR: backtest failed: {result.get('error', 'unknown')}")
        sys.exit(1)

    reports = result.get("reports", {})
    report = reports.get("full", {})
    sample_count = report.get("sampleCount", 0)
    print(f"Backtest complete: {sample_count} samples")

    # Extract bin-0 data from calibration bins and ROI
    cal_bins = report.get("calibrationByStat", {})
    roi_sim = report.get("roiSimulation", {})
    real_bins = report.get("realLineCalibBins", [])
    real_roi = report.get("roiReal", {})

    # For bin-0 analysis, we need per-bet PnL data.
    # The standard backtest doesn't expose per-bet detail, only aggregated bins.
    # We can reconstruct approximate PnL distribution from bin counts at -110 odds.
    #
    # Bin 0 = 0-10% probOver → UNDER bets at -110.
    # At -110: win = +$0.909, loss = -$1.00
    # So PnL list = [+0.909]*wins + [-1.0]*losses

    # Get real-line bin-0 if available, else synthetic bin-0
    bin0_real = None
    if real_bins and len(real_bins) > 0:
        b = real_bins[0]
        if b.get("betsPlaced", b.get("count", 0)) > 0:
            bin0_real = b

    bin0_synth = None
    # Aggregate bin-0 across all stats from roiSimulation
    # The exact bin-0 bet count is in realLineCalibBins[0] (for real-line subset)
    # For synthetic, we need the calibration bin data

    # Use whatever is available
    if bin0_real:
        n_bets = bin0_real.get("betsPlaced", bin0_real.get("count", 0))
        n_wins = bin0_real.get("wins", 0)
        pnl_total = bin0_real.get("pnlUnits", 0.0)
        source_label = "real closing lines"
    else:
        # Count bin-0 bets from synthetic calibration data
        # Since we can't directly get bin-0 bet counts from the standard output
        # when there are no real lines, we report overall numbers instead
        n_bets = roi_sim.get("betsPlaced", 0)
        n_wins = roi_sim.get("wins", 0)
        pnl_total = roi_sim.get("pnlUnits", 0.0)
        source_label = "all bins (synthetic; bin-0 breakdown unavailable)"

    n_losses = n_bets - n_wins

    # Reconstruct per-bet PnL at -110 odds for bootstrap
    # win at -110 = +0.9091, loss = -1.0
    win_pnl = 100.0 / 110.0  # +0.9091
    pnl_list = [win_pnl] * n_wins + [-1.0] * n_losses

    if not pnl_list:
        print("ERROR: No bets found for analysis.")
        sys.exit(1)

    # Bootstrap CI on ROI
    mean_roi, ci_lo, ci_hi = _bootstrap_ci(pnl_list, args.n_bootstrap, args.ci_level)
    mean_roi_pct = mean_roi * 100
    ci_lo_pct = ci_lo * 100
    ci_hi_pct = ci_hi * 100

    # Kill switch verdict
    if ci_lo > 0:
        verdict = "PROCEED"
        verdict_detail = f"CI entirely above zero [{ci_lo_pct:+.2f}%, {ci_hi_pct:+.2f}%]. Signal is real."
    elif mean_roi > 0:
        verdict = "CAUTIOUS"
        verdict_detail = f"ROI positive ({mean_roi_pct:+.2f}%) but CI includes zero [{ci_lo_pct:+.2f}%, {ci_hi_pct:+.2f}%]. Proceed with 2.2 only."
    else:
        verdict = "STOP"
        verdict_detail = f"ROI negative or CI entirely below zero [{ci_lo_pct:+.2f}%, {ci_hi_pct:+.2f}%]. Signal is not real."

    hit_rate = (n_wins / n_bets * 100) if n_bets > 0 else 0

    # Per-stat bin-0 breakdown from calibration data
    stat_bin0 = {}
    for stat, bins in cal_bins.items():
        if bins and len(bins) > 0:
            b0 = bins[0]  # 0-10% bin
            cnt = b0.get("count", 0)
            if cnt > 0:
                stat_bin0[stat] = {
                    "count": cnt,
                    "avgPredPct": b0.get("avgPredOverProbPct"),
                    "actualHitPct": b0.get("actualOverHitRatePct"),
                }

    # Print summary
    print()
    print(f"=== BIN-0 KILL SWITCH VERDICT: {verdict} ===")
    print(f"  Source: {source_label}")
    print(f"  Bets: {n_bets} | Wins: {n_wins} | Hit Rate: {hit_rate:.1f}%")
    print(f"  Mean ROI: {mean_roi_pct:+.2f}%/bet")
    print(f"  {args.ci_level:.0%} CI: [{ci_lo_pct:+.2f}%, {ci_hi_pct:+.2f}%]")
    print(f"  {verdict_detail}")
    print()
    print("Per-stat bin-0 calibration:")
    for stat, info in sorted(stat_bin0.items()):
        print(f"  {stat:5s}: n={info['count']:4d}  pred={info['avgPredPct']:.1f}%  actual={info['actualHitPct']:.1f}%")

    # Generate report
    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    report_lines = [
        f"# Bin-0 Walk-Forward Kill Switch Report",
        f"",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"",
        f"## Configuration",
        f"",
        f"| Parameter | Value |",
        f"|-----------|-------|",
        f"| Date range | {args.date_from} to {args.date_to} |",
        f"| Walk-forward | {args.walk_forward} |",
        f"| Odds source | {args.odds_source or 'synthetic (-110/-110)'} |",
        f"| Bootstrap resamples | {args.n_bootstrap:,} |",
        f"| CI level | {args.ci_level:.0%} |",
        f"| Total backtest samples | {sample_count:,} |",
        f"",
        f"## Results",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Source | {source_label} |",
        f"| Bin-0 bets | {n_bets:,} |",
        f"| Wins | {n_wins:,} |",
        f"| Hit rate | {hit_rate:.1f}% |",
        f"| Mean ROI | {mean_roi_pct:+.2f}%/bet |",
        f"| {args.ci_level:.0%} CI lower | {ci_lo_pct:+.2f}% |",
        f"| {args.ci_level:.0%} CI upper | {ci_hi_pct:+.2f}% |",
        f"",
        f"## Per-Stat Bin-0 Calibration",
        f"",
        f"| Stat | Count | Avg Predicted | Actual Hit Rate |",
        f"|------|-------|---------------|-----------------|",
    ]
    for stat, info in sorted(stat_bin0.items()):
        report_lines.append(
            f"| {stat} | {info['count']} | {info['avgPredPct']:.1f}% | {info['actualHitPct']:.1f}% |"
        )
    report_lines.extend([
        f"",
        f"## Verdict",
        f"",
        f"**{verdict}**: {verdict_detail}",
        f"",
    ])
    if verdict == "PROCEED":
        report_lines.append("Proceed to Phase 2.2-2.5 (signal amplification).")
    elif verdict == "CAUTIOUS":
        report_lines.append("Proceed to Phase 2.2 only (ast-specific analysis). Do not proceed to 2.3/2.5.")
    else:
        report_lines.extend([
            "**Do not proceed to Phase 2.2-2.5.**",
            "",
            "Options:",
            "- Accept breakeven model, use for CLV-filtered paper trading only",
            "- Investigate fundamentally different approach",
            "- Shut down the betting project",
        ])

    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines) + "\n")

    print(f"\nReport saved: {args.output}")

    # Also dump machine-readable JSON
    json_out = args.output.replace(".md", ".json")
    json_data = {
        "verdict": verdict,
        "dateFrom": args.date_from,
        "dateTo": args.date_to,
        "walkForward": args.walk_forward,
        "oddsSource": args.odds_source,
        "totalSamples": sample_count,
        "bin0Bets": n_bets,
        "bin0Wins": n_wins,
        "bin0HitRate": round(hit_rate, 2),
        "meanRoiPct": round(mean_roi_pct, 4),
        "ciLowerPct": round(ci_lo_pct, 4),
        "ciUpperPct": round(ci_hi_pct, 4),
        "ciLevel": args.ci_level,
        "nBootstrap": args.n_bootstrap,
        "source": source_label,
        "statBin0": stat_bin0,
    }
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2)
    print(f"JSON saved: {json_out}")


if __name__ == "__main__":
    main()
