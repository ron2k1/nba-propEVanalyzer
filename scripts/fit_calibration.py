#!/usr/bin/env python3
"""
Fit per-stat temperature-scaling calibration from a backtest result file.

Usage:
    python scripts/fit_calibration.py \
        --input  data/backtest_results/2026-01-26_to_2026-02-25_full_local.json \
        --output models/prob_calibration.json \
        [--min-count 50] [--min-pred 0.10] [--max-pred 0.90]

Output: models/prob_calibration.json
    { "pts": 1.65, "reb": 1.45, ..., "_fitted_on": "...", "_global": 1.50 }

Temperature T > 1 shrinks probabilities toward 50%:
    p_cal = sigmoid(logit(p_raw) / T)
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _sigmoid(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def _logit(p, eps=1e-9):
    p = max(eps, min(1.0 - eps, p))
    return math.log(p / (1.0 - p))


def _apply_temp(p_raw, T, eps=1e-9):
    return _sigmoid(_logit(p_raw, eps) / T)


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------

def fit_temperature(bins, min_count=50, min_pred=0.10, max_pred=0.90, min_T=1.0):
    """
    Grid-search temperature T that minimises count-weighted MSE between
    calibrated predicted probability and observed hit rate.

    Returns (best_T, best_mse, n_bins_used).
    """
    reliable = []
    for b in bins:
        pred_pct = b.get("avgPredOverProbPct")
        actual_pct = b.get("actualOverHitRatePct")
        n = b.get("count", 0)
        if pred_pct is None or actual_pct is None:
            continue
        p_pred = pred_pct / 100.0
        p_act = actual_pct / 100.0
        if n < min_count or not (min_pred <= p_pred <= max_pred):
            continue
        reliable.append((p_pred, p_act, n))

    if not reliable:
        return 1.0, None, 0

    total_n = sum(n for _, _, n in reliable)

    def _wmse(T):
        return sum(
            n * (_apply_temp(p, T) - a) ** 2
            for p, a, n in reliable
        ) / total_n

    best_T = max(1.0, min_T)
    best_mse = _wmse(best_T)

    # Coarse scan: T in [min_T, 5.00], step 0.05
    start_int = max(int(round(min_T * 100)), 70)
    for t_int in range(start_int, 501, 5):
        T = t_int / 100.0
        mse = _wmse(T)
        if mse < best_mse:
            best_mse = mse
            best_T = T

    # Fine scan: ±0.20 around best, step 0.01
    lo = max(min_T, best_T - 0.20)
    hi = min(5.00, best_T + 0.20)
    t_int_lo = int(round(lo * 100))
    t_int_hi = int(round(hi * 100))
    for t_int in range(t_int_lo, t_int_hi + 1):
        T = t_int / 100.0
        mse = _wmse(T)
        if mse < best_mse:
            best_mse = mse
            best_T = T

    return round(best_T, 2), round(best_mse * 10000, 2), len(reliable)


def _preview_calibration(bins, T, min_count=50, min_pred=0.10, max_pred=0.90):
    """Print before/after table for a stat."""
    rows = []
    for b in bins:
        pred_pct = b.get("avgPredOverProbPct")
        actual_pct = b.get("actualOverHitRatePct")
        n = b.get("count", 0)
        if pred_pct is None or actual_pct is None:
            continue
        p_raw = pred_pct / 100.0
        p_cal = _apply_temp(p_raw, T)
        rows.append((
            b.get("bin", "?"),
            n,
            pred_pct,
            actual_pct,
            round(p_cal * 100, 1),
            round(pred_pct - actual_pct, 1),
            round(p_cal * 100 - actual_pct, 1),
        ))
    print(f"  {'bin':10s} {'n':>6}  {'raw':>6}  {'actual':>7}  {'cal':>7}  {'raw_gap':>8}  {'cal_gap':>8}")
    for bin_lbl, n, raw, actual, cal, raw_gap, cal_gap in rows:
        flag = " *" if abs(cal_gap) > 10 else ""
        print(f"  {bin_lbl:10s} {n:6d}  {raw:6.1f}%  {actual:6.1f}%  {cal:6.1f}%  {raw_gap:+7.1f}  {cal_gap:+7.1f}{flag}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fit temperature-scaling calibration from backtest results.")
    parser.add_argument("--input", default=os.path.join(ROOT, "data", "backtest_results", "2026-01-26_to_2026-02-25_full_local.json"))
    parser.add_argument("--output", default=os.path.join(ROOT, "models", "prob_calibration.json"))
    parser.add_argument("--min-count", type=int, default=50)
    parser.add_argument("--min-pred", type=float, default=0.10)
    parser.add_argument("--max-pred", type=float, default=0.90)
    parser.add_argument("--model", default="full", help="Which model report to use (full|simple)")
    args = parser.parse_args()

    # --- Load backtest result ---
    print(f"Loading: {args.input}")
    with open(args.input) as f:
        data = json.load(f)

    reports = data.get("reports", {})
    report = reports.get(args.model)
    if not report:
        # Fallback: treat top-level as report if it has calibrationByStat
        report = data
    cal_by_stat = report.get("calibrationByStat", {})
    if not cal_by_stat:
        print("ERROR: no calibrationByStat found in report")
        sys.exit(1)

    print(f"Stats found: {list(cal_by_stat.keys())}")
    print(f"Fitting with: min_count={args.min_count}, pred in [{args.min_pred:.0%}, {args.max_pred:.0%}]")
    print()

    result = {
        "_fitted_on": args.input,
        "_fitted_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "_min_count": args.min_count,
    }

    all_T = []
    for stat, bins in cal_by_stat.items():
        T, mse, n_bins = fit_temperature(
            bins,
            min_count=args.min_count,
            min_pred=args.min_pred,
            max_pred=args.max_pred,
            min_T=1.0,  # Never anti-calibrate (never expand probs away from 50%)
        )
        result[stat] = T
        all_T.append(T)
        mse_str = f"MSE*10^4={mse:.2f}" if mse is not None else "no data"
        print(f"=== {stat.upper():5s}  T={T:.2f}  ({mse_str}, {n_bins} bins) ===")
        _preview_calibration(bins, T, args.min_count, args.min_pred, args.max_pred)
        print()

    # Global fallback (median T)
    if all_T:
        all_T_sorted = sorted(all_T)
        mid = len(all_T_sorted) // 2
        global_T = all_T_sorted[mid]
        result["_global"] = global_T
        print(f"Global fallback T (median): {global_T:.2f}")

    # --- Save ---
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nSaved: {args.output}")
    print(json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, indent=2))


if __name__ == "__main__":
    main()
