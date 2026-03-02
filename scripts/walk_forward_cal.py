#!/usr/bin/env python3
"""
Generate expanding-window walk-forward calibration files.

For each target date D (sampled every --step days from season_start to end_date):
  1. Run a local backtest from season_start to D-1 (training window).
  2. Extract calibrationByStat from the result.
  3. Fit temperatures using fit_calibration functions.
  4. Save to models/walk_forward/prob_cal_{D}.json with _sample_counts.

Usage:
    python scripts/walk_forward_cal.py \\
        --season-start 2025-10-21 \\
        --end-date 2026-02-25 \\
        --step 7 \\
        --min-samples 200 \\
        --output-dir models/walk_forward/

The _load_prob_calibration_for_date() function in nba_ev_engine.py will
automatically find the latest file whose date <= as_of_date.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.nba_backtest import run_backtest
from scripts.fit_calibration import fit_temperature, fit_bin_temperatures


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _date_str(d):
    return d.strftime("%Y-%m-%d")


def _fit_from_cal_by_stat(
    cal_by_stat,
    min_count=50,
    bin_min_count=30,
    min_pred=0.01,
    max_pred=0.25,
):
    """
    Fit temperatures from a calibrationByStat dict (already aggregated bins).

    Returns the calibration result dict (without metadata fields).
    """
    result = {}
    all_T = []
    sample_counts = {}

    for stat, bins in cal_by_stat.items():
        stat_total = sum(b.get("count", 0) for b in bins)
        sample_counts[stat] = stat_total

        T, _mse, _n = fit_temperature(
            bins,
            min_count=min_count,
            min_pred=min_pred,
            max_pred=max_pred,
            min_T=1.0,
        )
        result[stat] = T
        all_T.append(T)

        bin_temps = fit_bin_temperatures(
            bins,
            min_count=bin_min_count,
            min_pred=min_pred,
            max_pred=max_pred,
        )
        if bin_temps:
            result[f"{stat}_bins"] = bin_temps

    # Global fallback: median T across stats
    if all_T:
        all_T_sorted = sorted(all_T)
        mid = len(all_T_sorted) // 2
        result["_global"] = all_T_sorted[mid]
    else:
        result["_global"] = 1.0

    return result, sample_counts


def _generate_dates(season_start, end_date, step):
    """
    Yield target dates D from season_start+step to end_date (inclusive),
    stepping by `step` days.

    Training window for each D: [season_start, D-1].
    D itself is the first date the calibration is valid for (backtest sees D-1 data).
    """
    # First viable target date: season_start + step (need at least `step` days of training data)
    current = season_start + timedelta(days=step)
    while current <= end_date:
        yield current
        current += timedelta(days=step)


def main():
    parser = argparse.ArgumentParser(
        description="Generate expanding-window walk-forward calibration files."
    )
    parser.add_argument(
        "--season-start", default="2025-10-21",
        help="First date of the NBA season (training window starts here)."
    )
    parser.add_argument(
        "--end-date", required=True,
        help="Last target date to generate calibration for (YYYY-MM-DD). "
             "Must be strictly before today (no-lookahead)."
    )
    parser.add_argument(
        "--step", type=int, default=7,
        help="Days between calibration files (default: 7 = weekly)."
    )
    parser.add_argument(
        "--min-samples", type=int, default=200,
        help="Minimum backtest samples for a stat-specific temperature to be emitted. "
             "Stats below this threshold are omitted; _global is used instead."
    )
    parser.add_argument(
        "--min-count", type=int, default=50,
        help="Min samples per calibration bin for global-T fitting (passed to fit_temperature)."
    )
    parser.add_argument(
        "--bin-min-count", type=int, default=30,
        help="Min samples per bin for piecewise fitting (passed to fit_bin_temperatures)."
    )
    parser.add_argument(
        "--min-pred", type=float, default=0.01,
        help="Min predicted probability for bins included in fitting."
    )
    parser.add_argument(
        "--max-pred", type=float, default=0.25,
        help="Max predicted probability for bins included in fitting."
    )
    parser.add_argument(
        "--output-dir", default=os.path.join(ROOT, "models", "walk_forward"),
        help="Directory to write prob_cal_YYYY-MM-DD.json files."
    )
    parser.add_argument(
        "--model", default="full",
        help="Backtest model to use (full|simple|both). Calibration uses --model report."
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing calibration files (default: skip existing)."
    )
    args = parser.parse_args()

    season_start = _parse_date(args.season_start)
    end_date = _parse_date(args.end_date)
    today = date.today()

    if season_start is None:
        print(f"ERROR: invalid --season-start: {args.season_start}")
        sys.exit(1)
    if end_date is None:
        print(f"ERROR: invalid --end-date: {args.end_date}")
        sys.exit(1)
    if end_date >= today:
        print(f"ERROR: --end-date {end_date} must be strictly before today ({today}) [no-lookahead rule].")
        sys.exit(1)
    if args.step < 1:
        print("ERROR: --step must be >= 1")
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)

    target_dates = list(_generate_dates(season_start, end_date, args.step))
    if not target_dates:
        print(f"No target dates generated. Need at least {args.step} days between season-start and end-date.")
        sys.exit(0)

    print(f"Walk-forward calibration: {len(target_dates)} target dates")
    print(f"  season_start={season_start}  end_date={end_date}  step={args.step}d")
    print(f"  min_samples={args.min_samples}  output_dir={args.output_dir}")
    print()

    success_count = 0
    skip_count = 0
    fail_count = 0

    for target_date in target_dates:
        date_str = _date_str(target_date)
        out_path = os.path.join(args.output_dir, f"prob_cal_{date_str}.json")

        if os.path.isfile(out_path) and not args.overwrite:
            print(f"  [{date_str}] SKIP (file exists, use --overwrite to regenerate)")
            skip_count += 1
            continue

        # Training window: season_start to target_date - 1
        train_to = target_date - timedelta(days=1)
        train_from_str = _date_str(season_start)
        train_to_str = _date_str(train_to)

        print(f"  [{date_str}] Backtest {train_from_str} -> {train_to_str} ...", end="", flush=True)

        try:
            result = run_backtest(
                date_from=train_from_str,
                date_to=train_to_str,
                model=args.model,
                save_results=False,
                fast=True,
                data_source="local",
            )
        except Exception as exc:
            print(f" FAIL (run_backtest raised: {exc})")
            fail_count += 1
            continue

        if not result.get("success"):
            err = result.get("error", "unknown error")
            print(f" FAIL ({err})")
            fail_count += 1
            continue

        # Extract calibrationByStat from the model report
        reports = result.get("reports", {})
        report = reports.get(args.model) or result
        cal_by_stat = report.get("calibrationByStat", {})

        if not cal_by_stat:
            print(f" FAIL (no calibrationByStat in result)")
            fail_count += 1
            continue

        sample_count = report.get("sampleCount", 0)
        print(f" ok (sampleCount={sample_count})", flush=True)

        # Fit temperatures
        cal_result, sample_counts = _fit_from_cal_by_stat(
            cal_by_stat,
            min_count=args.min_count,
            bin_min_count=args.bin_min_count,
            min_pred=args.min_pred,
            max_pred=args.max_pred,
        )

        # Apply min-sample gate: remove stats with insufficient data
        # (nba_ev_engine._load_prob_calibration_for_date also does this, but
        # we do it here too so the files are self-documenting)
        pruned_stats = []
        for stat_k in list(cal_result.keys()):
            if stat_k.startswith("_") or stat_k.endswith("_bins"):
                continue
            if sample_counts.get(stat_k, 0) < args.min_samples:
                cal_result.pop(stat_k, None)
                cal_result.pop(f"{stat_k}_bins", None)
                pruned_stats.append(stat_k)

        if pruned_stats:
            print(f"    min_sample gate removed: {pruned_stats} (< {args.min_samples} samples)")

        # Build output dict
        output = {
            "_fitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "_train_from": train_from_str,
            "_train_to": train_to_str,
            "_for_date": date_str,
            "_min_samples": args.min_samples,
            "_sample_counts": sample_counts,
        }
        output.update(cal_result)

        with open(out_path, "w") as f:
            json.dump(output, f, indent=2)

        kept_stats = [k for k in cal_result if not k.startswith("_") and not k.endswith("_bins")]
        print(f"    Saved: {out_path}")
        print(f"    Stats: {kept_stats}  _global={cal_result.get('_global', 'n/a')}")
        success_count += 1

    print()
    print(f"Done: {success_count} written, {skip_count} skipped, {fail_count} failed")


if __name__ == "__main__":
    main()
