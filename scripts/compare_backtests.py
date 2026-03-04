#!/usr/bin/env python3
"""
Side-by-side comparison of two backtest JSON result files.

Usage:
    python scripts/compare_backtests.py <baseline.json> <matchlive.json>

Produces:
    - Aggregate metrics table
    - Per-stat breakdown
    - Per-bin breakdown
    - Accounting invariant assertion (exits non-zero on failure)
"""

import json
import sys
from datetime import datetime


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _rpt(data, model="full"):
    """Extract the report dict for a given model from the backtest JSON."""
    reports = data.get("reports", {})
    if model in reports:
        return reports[model]
    # Single-model run: reports may have only one key
    for k, v in reports.items():
        return v
    return {}


def _safe_div(a, b, digits=3):
    if b is None or b == 0:
        return None
    return round(a / b, digits)


def _delta_str(a, b):
    """Format delta between two values, handling None."""
    if a is None or b is None:
        return "N/A"
    d = b - a
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.3f}"


def _pct_delta_str(a, b):
    if a is None or b is None:
        return "N/A"
    d = b - a
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.1f}%"


def _print_table(headers, rows, col_widths=None):
    if col_widths is None:
        col_widths = []
        for i, h in enumerate(headers):
            w = len(str(h))
            for row in rows:
                if i < len(row):
                    w = max(w, len(str(row[i])))
            col_widths.append(w + 2)

    header_line = "".join(str(h).ljust(col_widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        print("".join(str(row[i]).ljust(col_widths[i]) for i in range(len(headers))))
    print()


def main():
    if len(sys.argv) < 3:
        print("Usage: compare_backtests.py <baseline.json> <matchlive.json>")
        sys.exit(1)

    baseline_path = sys.argv[1]
    matchlive_path = sys.argv[2]

    baseline = _load(baseline_path)
    matchlive = _load(matchlive_path)

    b_rpt = _rpt(baseline)
    m_rpt = _rpt(matchlive)

    print("=" * 70)
    print("BACKTEST COMPARISON REPORT")
    print(f"  Baseline:   {baseline_path}")
    print(f"  Match-Live: {matchlive_path}")
    print(f"  Date range: {baseline.get('dateFrom')} -> {baseline.get('dateTo')}")
    print(f"  Generated:  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)
    print()

    # --- Aggregate Table ---
    b_roi_sim = b_rpt.get("roiSimulation", {})
    m_roi_sim = m_rpt.get("roiSimulation", {})
    b_roi_real = b_rpt.get("roiReal", {})
    m_roi_real = m_rpt.get("roiReal", {})

    b_signals = b_roi_sim.get("betsPlaced", 0)
    m_signals = m_roi_sim.get("betsPlaced", 0)
    days = max(baseline.get("days", 1), 1)

    rows = [
        ("Signal count", b_signals, m_signals, _delta_str(b_signals, m_signals)),
        ("Signals/day", round(b_signals / days, 2), round(m_signals / days, 2),
         _delta_str(b_signals / days, m_signals / days)),
        ("ROI (simulation)", b_roi_sim.get("roiPctPerBet", "N/A"),
         m_roi_sim.get("roiPctPerBet", "N/A"),
         _pct_delta_str(b_roi_sim.get("roiPctPerBet"), m_roi_sim.get("roiPctPerBet"))),
        ("Hit rate", b_roi_sim.get("hitRatePct", "N/A"),
         m_roi_sim.get("hitRatePct", "N/A"),
         _pct_delta_str(b_roi_sim.get("hitRatePct"), m_roi_sim.get("hitRatePct"))),
        ("ROI (real-line)", b_roi_real.get("roiPctPerBet", "N/A"),
         m_roi_real.get("roiPctPerBet", "N/A"),
         _pct_delta_str(b_roi_real.get("roiPctPerBet"), m_roi_real.get("roiPctPerBet"))),
        ("Real-line hit rate", b_roi_real.get("hitRatePct", "N/A"),
         m_roi_real.get("hitRatePct", "N/A"),
         _pct_delta_str(b_roi_real.get("hitRatePct"), m_roi_real.get("hitRatePct"))),
        ("Real-line samples", b_rpt.get("realLineSamples", 0),
         m_rpt.get("realLineSamples", 0),
         _delta_str(b_rpt.get("realLineSamples", 0), m_rpt.get("realLineSamples", 0))),
        ("Avg edge", "—", "—", "—"),
        ("Gates unavailable", "N/A",
         m_rpt.get("matchLiveGatesUnavailable", 0), "—"),
    ]

    print("AGGREGATE METRICS")
    _print_table(["Metric", "Baseline", "Match-Live", "Delta"], rows)

    # Gate rejection breakdown
    ml_rejections = m_rpt.get("matchLiveRejections", {})
    if ml_rejections:
        print("GATE REJECTION BREAKDOWN (Match-Live)")
        rej_rows = sorted(ml_rejections.items(), key=lambda x: -x[1])
        _print_table(
            ["Rejection Reason", "Count"],
            [(reason, count) for reason, count in rej_rows],
        )

    # --- Per-Stat Breakdown ---
    stats = ["pts", "reb", "ast", "fg3m", "pra", "stl", "blk", "tov"]

    # Build per-stat data from bet-level records if available, else from realLineStatRoi
    b_stat_roi = b_rpt.get("realLineStatRoi", {})
    m_stat_roi = m_rpt.get("realLineStatRoi", {})

    stat_rows = []
    for s in stats:
        b_sr = b_stat_roi.get(s, {})
        m_sr = m_stat_roi.get(s, {})
        b_n = b_sr.get("betsPlaced", 0)
        m_n = m_sr.get("betsPlaced", 0)
        if b_n == 0 and m_n == 0:
            continue
        stat_rows.append((
            s,
            b_n, m_n,
            b_sr.get("roiPctPerBet", "N/A"),
            m_sr.get("roiPctPerBet", "N/A"),
            b_sr.get("hitRatePct", "N/A"),
            m_sr.get("hitRatePct", "N/A"),
        ))

    if stat_rows:
        print("PER-STAT BREAKDOWN (real-line bets)")
        _print_table(
            ["Stat", "Base Signals", "ML Signals", "Base ROI%", "ML ROI%", "Base Hit%", "ML Hit%"],
            stat_rows,
        )

    # --- Per-Bin Breakdown ---
    b_bins = b_rpt.get("realLineCalibBins", [])
    m_bins = m_rpt.get("realLineCalibBins", [])

    bin_rows = []
    for i in range(min(len(b_bins), len(m_bins))):
        bb = b_bins[i] if i < len(b_bins) else {}
        mb = m_bins[i] if i < len(m_bins) else {}
        b_n = bb.get("betsPlaced", 0)
        m_n = mb.get("betsPlaced", 0)
        if b_n == 0 and m_n == 0:
            continue
        bin_rows.append((
            bb.get("bin", f"{i*10}-{(i+1)*10}%"),
            b_n, m_n,
            bb.get("roiPctPerBet", "N/A"),
            mb.get("roiPctPerBet", "N/A"),
            bb.get("hitRatePct", "N/A"),
            mb.get("hitRatePct", "N/A"),
        ))

    if bin_rows:
        print("PER-BIN BREAKDOWN (real-line bets)")
        _print_table(
            ["Bin", "Base Signals", "ML Signals", "Base ROI%", "ML ROI%", "Base Hit%", "ML Hit%"],
            bin_rows,
        )

    # --- Accounting Invariant ---
    ml_total_eval = m_rpt.get("matchLiveTotalEvaluated", 0)
    ml_rejected = sum(ml_rejections.values()) if ml_rejections else 0
    # "passed" means: qualified via 10-gate AND had positive EV
    # betsPlaced in roiSimulation counts policy-passing bets
    ml_passed = m_roi_sim.get("betsPlaced", 0)

    # Accounting: rejected + negative_ev_skipped + passed = total_evaluated
    # We only track rejected (by gates) and passed (policy_pass=True).
    # Bets with positive EV that passed gates = betsPlaced.
    # Bets rejected by gates = ml_rejected.
    # Bets not rejected by gates but with negative EV = total - rejected - passed.
    # So: rejected + passed <= total_evaluated (the gap is negative-EV bets that passed gates).
    print("ACCOUNTING CHECK")
    if ml_total_eval > 0:
        gap = ml_total_eval - ml_rejected - ml_passed
        if gap >= 0:
            print(
                f"  {ml_rejected} rejected + {ml_passed} passed + "
                f"{gap} negative-EV = {ml_total_eval} evaluated OK"
            )
        else:
            print(
                f"  ACCOUNTING ERROR: {ml_rejected} rejected + {ml_passed} passed "
                f"= {ml_rejected + ml_passed} > {ml_total_eval} evaluated "
                f"-- bets leaked!"
            )
            sys.exit(1)
    else:
        print("  No match-live evaluation data (matchLiveTotalEvaluated=0)")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
