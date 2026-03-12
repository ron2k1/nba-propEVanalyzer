#!/usr/bin/env python3
"""
Fit per-stat temperature-scaling calibration from a backtest result file.

Usage:
    python scripts/fit_calibration.py \
        --input  data/backtest_results/2026-01-26_to_2026-02-25_full_local.json \
        --output models/prob_calibration.json \
        [--min-count 50] [--bin-min-count 30] [--min-pred 0.10] [--max-pred 0.90]

Output: models/prob_calibration.json
    {
      "pts": 1.65,               # global temperature (shrinks all bins equally)
      "pts_bins": {              # per-bin temperatures (piecewise calibration, #4)
        "30-40": 1.45,
        "70-80": 3.10,
        ...
      },
      "_fitted_on": "...", "_global": 1.50
    }

Temperature T > 1 shrinks probabilities toward 50%:
    p_cal = sigmoid(logit(p_raw) / T)

Per-bin temperatures are fit independently per 10% probability bucket using
Pool Adjacent Violators (PAV) isotonic regression to ensure monotonicity of
calibrated outputs. bin-specific T takes precedence over global T in the
EV engine.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

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
# Piecewise (per-bin) calibration helpers  [#4]
# ---------------------------------------------------------------------------

def _fit_bin_temp(p_raw, p_actual, min_T=1.0, max_T=8.0):
    """
    Grid-search T in [min_T, max_T] that minimises |apply_temp(p_raw, T) - p_actual|.
    Always shrinks toward 50% (T >= 1); never anti-calibrates.
    """
    if p_raw <= 0.01 or p_raw >= 0.99:
        return 1.0
    best_T, best_err = 1.0, abs(_apply_temp(p_raw, 1.0) - p_actual)
    # Coarse pass: step 0.05
    for t_int in range(int(min_T * 100), int(max_T * 100) + 1, 5):
        T = t_int / 100.0
        err = abs(_apply_temp(p_raw, T) - p_actual)
        if err < best_err:
            best_err, best_T = err, T
    # Fine pass: ±0.20 around best, step 0.01
    lo = max(int(min_T * 100), int((best_T - 0.20) * 100))
    hi = min(int(max_T * 100), int((best_T + 0.20) * 100))
    for t_int in range(lo, hi + 1):
        T = t_int / 100.0
        err = abs(_apply_temp(p_raw, T) - p_actual)
        if err < best_err:
            best_err, best_T = err, T
    return round(best_T, 2)


def _weighted_avg(block):
    """Count-weighted average of p_actual across a PAV block."""
    return sum(x[1] * x[2] for x in block) / sum(x[2] for x in block)


def _pav_weighted_bins(items):
    """
    Pool Adjacent Violators (PAV) isotonic regression — non-decreasing p_actual.
    items: list of [p_raw, p_actual, count, bin_lbl] sorted by p_raw.
    Returns: list of blocks, each block being a list of the original items merged into it.
    """
    blocks = [[item] for item in items]
    i = 0
    while i < len(blocks) - 1:
        if _weighted_avg(blocks[i]) > _weighted_avg(blocks[i + 1]):
            blocks[i:i + 2] = [blocks[i] + blocks[i + 1]]
            if i > 0:
                i -= 1
        else:
            i += 1
    return blocks


def fit_bin_temperatures(bins, min_count=30, min_pred=0.10, max_pred=0.90):
    """
    Fit per-bin temperatures from backtest calibration bins.

    Returns dict like {"70-80": 3.10, "60-70": 1.40, ...} using keys WITHOUT
    the trailing '%' (matching the EV engine lookup format), or None if < 2 bins
    have sufficient data.

    min_pred / max_pred: filter out bins whose average predicted probability
    falls outside this range (mirrors the same filter in fit_temperature).
    """
    eligible = []
    for b in bins:
        pred_pct = b.get("avgPredOverProbPct")
        actual_pct = b.get("actualOverHitRatePct")
        n = b.get("count", 0)
        bin_lbl = b.get("bin", "").rstrip("%")
        if pred_pct is None or actual_pct is None or n < min_count:
            continue
        p_pred = pred_pct / 100.0
        if not (min_pred <= p_pred <= max_pred):
            continue
        eligible.append([p_pred, actual_pct / 100.0, n, bin_lbl])

    if len(eligible) < 2:
        return None

    eligible.sort(key=lambda x: x[0])

    # Isotonic regression to enforce monotone non-decreasing actuals
    blocks = _pav_weighted_bins(eligible)

    result = {}
    for block in blocks:
        total_count = sum(x[2] for x in block)
        p_raw_w = sum(x[0] * x[2] for x in block) / total_count
        p_actual_w = sum(x[1] * x[2] for x in block) / total_count
        T_bin = _fit_bin_temp(p_raw_w, p_actual_w)
        for _, _, _, bin_lbl in block:
            result[bin_lbl] = T_bin

    # Post-process: enforce monotone calibrated outputs across bins.
    # Independent per-bin T fitting can produce non-monotone calibrated
    # probabilities (e.g., high T at low bins overshoots mid-bin outputs).
    # Fix by merging adjacent violating bins until outputs are monotone.
    result = _enforce_monotone_bin_outputs(result)

    return result or None


def _enforce_monotone_bin_outputs(bin_temps):
    """Merge adjacent bins whose calibrated midpoint outputs violate monotonicity."""
    if not bin_temps:
        return bin_temps

    # Parse bin labels into (lo, hi, label, T)
    parsed = []
    for lbl, T in bin_temps.items():
        parts = lbl.split("-")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            parsed.append([int(parts[0]), int(parts[1]), lbl, T])
    parsed.sort(key=lambda x: x[0])

    if len(parsed) < 2:
        return bin_temps

    # Iteratively merge violating adjacent bins
    changed = True
    while changed:
        changed = False
        cal_vals = [_apply_temp((p[0] + p[1]) / 200.0, p[3]) for p in parsed]
        for i in range(1, len(cal_vals)):
            if cal_vals[i] < cal_vals[i - 1] - 0.001:
                # Average the T values of the two violating bins
                avg_T = round((parsed[i - 1][3] + parsed[i][3]) / 2, 2)
                parsed[i - 1][3] = avg_T
                parsed[i][3] = avg_T
                changed = True
                break

    return {item[2]: item[3] for item in parsed}


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
    parser.add_argument("--bin-min-count", type=int, default=30, help="Min samples per bin for piecewise calibration (#4)")
    parser.add_argument("--min-pred", type=float, default=0.10)
    parser.add_argument("--max-pred", type=float, default=0.90)
    parser.add_argument("--model", default="full", help="Which model report to use (full|simple)")
    parser.add_argument("--no-piecewise", action="store_true", help="Skip per-bin temperature fitting")
    parser.add_argument("--min-samples", type=int, default=None,
                        help="If set, emit _sample_counts in output; stats below this threshold use _global T.")
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
    if not args.no_piecewise:
        print(f"Piecewise (#4): bin_min_count={args.bin_min_count}")
    print()

    result = {
        "_fitted_on": args.input,
        "_fitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "_min_count": args.min_count,
    }
    if args.min_samples is not None:
        result["_min_samples"] = args.min_samples

    all_T = []
    sample_counts = {}
    for stat, bins in cal_by_stat.items():
        # Compute total sample count for this stat (sum across all bins)
        stat_total = sum(b.get("count", 0) for b in bins)
        sample_counts[stat] = stat_total

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
        print(f"=== {stat.upper():5s}  T={T:.2f}  ({mse_str}, {n_bins} bins, n_total={stat_total}) ===")
        _preview_calibration(bins, T, args.min_count, args.min_pred, args.max_pred)

        # Piecewise (per-bin) calibration (#4)
        if not args.no_piecewise:
            bin_temps = fit_bin_temperatures(
                bins,
                min_count=args.bin_min_count,
                min_pred=args.min_pred,
                max_pred=args.max_pred,
            )
            if bin_temps:
                result[f"{stat}_bins"] = bin_temps
                print(f"  bin temps: {bin_temps}")
            else:
                print(f"  bin temps: insufficient data (need >= 2 bins with n >= {args.bin_min_count})")
        print()

    # Global fallback (median T)
    if all_T:
        all_T_sorted = sorted(all_T)
        mid = len(all_T_sorted) // 2
        global_T = all_T_sorted[mid]
        result["_global"] = global_T
        print(f"Global fallback T (median): {global_T:.2f}")

    # Emit _sample_counts when --min-samples is specified
    if args.min_samples is not None:
        result["_sample_counts"] = sample_counts
        print(f"Sample counts by stat: {sample_counts}")

    # --- Save ---
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nSaved: {args.output}")
    print(json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, indent=2))


if __name__ == "__main__":
    main()
