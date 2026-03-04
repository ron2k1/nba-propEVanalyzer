#!/usr/bin/env python3
"""
Calibration comparison: raw vs blended projections.

Answers the question: is blending destroying real signal or correctly
regularizing overconfident predictions?

Uses the calibrationByStat and Brier scores already in backtest JSON.
Then does matched-sample emit-bets analysis for the tail bins.
"""

import json
import sys


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _rpt(data, model="full"):
    reports = data.get("reports", {})
    if model in reports:
        return reports[model]
    for v in reports.values():
        return v
    return {}


def main():
    if len(sys.argv) < 3:
        print("Usage: calibration_blend_check.py <unblended.json> <blended.json>")
        print("  e.g. --a (no-blend) vs --b (blend), same gate system")
        sys.exit(1)

    raw_data = _load(sys.argv[1])
    blend_data = _load(sys.argv[2])
    raw_rpt = _rpt(raw_data)
    blend_rpt = _rpt(blend_data)

    raw_label = sys.argv[1].split("/")[-1].split("\\")[-1]
    blend_label = sys.argv[2].split("/")[-1].split("\\")[-1]

    print("=" * 78)
    print("CALIBRATION COMPARISON: RAW vs BLENDED PROJECTIONS")
    print(f"  Raw (no-blend): {raw_label}")
    print(f"  Blended:        {blend_label}")
    print(f"  Date range:     {raw_data.get('dateFrom')} -> {raw_data.get('dateTo')}")
    print("=" * 78)
    print()

    # ── Section 1: Brier Score Comparison ──
    # Lower Brier = better calibrated. This is the gold standard.
    raw_brier = raw_rpt.get("brierByStat", {})
    blend_brier = blend_rpt.get("brierByStat", {})

    print("SECTION 1: BRIER SCORES (lower = better calibrated)")
    print(f"{'Stat':<8}  {'Raw':>8}  {'Blended':>8}  {'Delta':>8}  {'Winner':>10}")
    print("-" * 50)
    betting_stats = ["pts", "ast", "reb", "pra"]
    all_stats = ["pts", "reb", "ast", "fg3m", "pra", "stl", "blk"]
    raw_wins = 0
    blend_wins = 0
    for s in all_stats:
        rb = raw_brier.get(s)
        bb = blend_brier.get(s)
        if rb is None or bb is None:
            continue
        delta = bb - rb  # positive = raw is better
        winner = "RAW" if delta > 0.001 else ("BLEND" if delta < -0.001 else "TIE")
        marker = " ***" if s in ["pts", "ast"] else ""
        if delta > 0.001:
            raw_wins += 1
        elif delta < -0.001:
            blend_wins += 1
        print(f"{s:<8}  {rb:>8.4f}  {bb:>8.4f}  {delta:>+8.4f}  {winner:>10}{marker}")
    print()
    print(f"  Raw wins: {raw_wins}   Blend wins: {blend_wins}")
    print(f"  *** = active betting stat")
    print()

    # ── Section 2: Calibration Curves — Full Sample ──
    # For each bin, compare |predicted - actual| between raw and blended
    print("SECTION 2: CALIBRATION CURVES (all 32K samples, not just bets)")
    print("  Shows: predicted P(over) vs actual hit rate. Closer = better.\n")

    for s in betting_stats:
        raw_cal = raw_rpt.get("calibrationByStat", {}).get(s, [])
        blend_cal = blend_rpt.get("calibrationByStat", {}).get(s, [])
        if not raw_cal or not blend_cal:
            continue

        print(f"  {s.upper()}")
        print(f"  {'Bin':<10} {'n_raw':>6} {'Pred%':>7} {'Act%':>7} {'Err':>7}  |  {'n_bld':>6} {'Pred%':>7} {'Act%':>7} {'Err':>7}  {'Better':>7}")
        print(f"  {'-'*95}")

        for i in range(min(len(raw_cal), len(blend_cal))):
            rc = raw_cal[i]
            bc = blend_cal[i]
            rn = rc.get("count", 0)
            bn = bc.get("count", 0)
            if rn == 0 and bn == 0:
                continue

            rp = rc.get("avgPredOverProbPct")
            ra = rc.get("actualOverHitRatePct")
            bp = bc.get("avgPredOverProbPct")
            ba = bc.get("actualOverHitRatePct")

            r_err = abs(rp - ra) if (rp is not None and ra is not None) else None
            b_err = abs(bp - ba) if (bp is not None and ba is not None) else None

            r_err_s = f"{r_err:>7.1f}" if r_err is not None else "    N/A"
            b_err_s = f"{b_err:>7.1f}" if b_err is not None else "    N/A"

            if r_err is not None and b_err is not None:
                winner = "RAW" if r_err < b_err - 0.5 else ("BLEND" if b_err < r_err - 0.5 else "~tie")
            else:
                winner = "?"

            rp_s = f"{rp:>7.1f}" if rp is not None else "    N/A"
            ra_s = f"{ra:>7.1f}" if ra is not None else "    N/A"
            bp_s = f"{bp:>7.1f}" if bp is not None else "    N/A"
            ba_s = f"{ba:>7.1f}" if ba is not None else "    N/A"

            bin_label = rc.get("bin", f"{i*10}-{(i+1)*10}%")
            # Highlight betting-relevant bins
            marker = " <<" if i in [0, 1, 8, 9] else ""
            print(f"  {bin_label:<10} {rn:>6} {rp_s} {ra_s} {r_err_s}  |  {bn:>6} {bp_s} {ba_s} {b_err_s}  {winner:>7}{marker}")
        print()

    # ── Section 3: Betting-Bin Focus ──
    # The bins where bets actually land: 0-10%, 10-20% (under), 80-90%, 90-100% (over)
    print("SECTION 3: BETTING-BIN CALIBRATION (only bins where bets land)")
    print("  These are the bins that determine ROI.\n")

    focus_bins = [0, 1, 8, 9]
    for s in ["pts", "ast", "reb"]:
        raw_cal = raw_rpt.get("calibrationByStat", {}).get(s, [])
        blend_cal = blend_rpt.get("calibrationByStat", {}).get(s, [])
        if not raw_cal or not blend_cal:
            continue

        print(f"  {s.upper()}")
        print(f"  {'Bin':<10} {'Raw: Pred->Act (err)':>25}  {'Blend: Pred->Act (err)':>25}  {'Verdict':>8}")
        print(f"  {'-'*75}")
        for i in focus_bins:
            if i >= len(raw_cal) or i >= len(blend_cal):
                continue
            rc = raw_cal[i]
            bc = blend_cal[i]
            rn = rc.get("count", 0)
            bn = bc.get("count", 0)

            rp = rc.get("avgPredOverProbPct")
            ra = rc.get("actualOverHitRatePct")
            bp = bc.get("avgPredOverProbPct")
            ba = bc.get("actualOverHitRatePct")

            if rp is not None and ra is not None:
                r_err = abs(rp - ra)
                r_str = f"{rp:.1f}->{ra:.1f} ({r_err:.1f}) n={rn}"
            else:
                r_err = None
                r_str = f"n={rn}"

            if bp is not None and ba is not None:
                b_err = abs(bp - ba)
                b_str = f"{bp:.1f}->{ba:.1f} ({b_err:.1f}) n={bn}"
            else:
                b_err = None
                b_str = f"n={bn}"

            if r_err is not None and b_err is not None:
                verdict = "RAW" if r_err < b_err - 0.5 else ("BLEND" if b_err < r_err - 0.5 else "~tie")
            else:
                verdict = "?"

            bin_label = rc.get("bin", f"{i*10}-{(i+1)*10}%")
            print(f"  {bin_label:<10} {r_str:>25}  {b_str:>25}  {verdict:>8}")
        print()

    # ── Section 4: Overconfidence Test ──
    # The user's specific question: if raw says P=X, is actual closer to X (raw right)
    # or closer to 50% (blend right)?
    print("SECTION 4: OVERCONFIDENCE TEST")
    print("  For tail bins: does actual outcome track raw prediction or regress toward 50%?\n")

    for s in ["pts", "ast"]:
        raw_cal = raw_rpt.get("calibrationByStat", {}).get(s, [])
        blend_cal = blend_rpt.get("calibrationByStat", {}).get(s, [])
        if not raw_cal:
            continue

        print(f"  {s.upper()}")
        for i in [0, 1]:
            rc = raw_cal[i]
            bc = blend_cal[i] if i < len(blend_cal) else {}
            rp = rc.get("avgPredOverProbPct")
            ra = rc.get("actualOverHitRatePct")
            bp = bc.get("avgPredOverProbPct")
            ba = bc.get("actualOverHitRatePct")
            rn = rc.get("count", 0)
            bn = bc.get("count", 0)

            if rp is None or ra is None:
                continue

            # How far is actual from raw prediction vs from 50%?
            dist_to_raw = abs(ra - rp)
            dist_to_50 = abs(ra - 50)
            bin_label = rc.get("bin", f"{i*10}-{(i+1)*10}%")

            print(f"    {bin_label}: raw predicts {rp:.1f}%, actual={ra:.1f}% (n={rn})")
            if bp is not None and ba is not None:
                print(f"    {' '*len(bin_label)}  blend predicts {bp:.1f}%, actual={ba:.1f}% (n={bn})")
            if ra < 50:
                # Actual is below 50% — raw was right to predict low
                print(f"    {' '*len(bin_label)}  Actual IS below 50% -> raw direction correct")
                if dist_to_raw < dist_to_50:
                    print(f"    {' '*len(bin_label)}  Actual closer to raw ({dist_to_raw:.1f}) than 50% ({dist_to_50:.1f}) -> RAW wins")
                else:
                    print(f"    {' '*len(bin_label)}  Actual closer to 50% ({dist_to_50:.1f}) than raw ({dist_to_raw:.1f}) -> overconfident")
            print()

    # ── Summary ──
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)

    # Count Brier wins for betting stats only
    betting_raw_wins = 0
    betting_blend_wins = 0
    for s in ["pts", "ast"]:
        rb = raw_brier.get(s)
        bb = blend_brier.get(s)
        if rb is not None and bb is not None:
            if rb < bb:
                betting_raw_wins += 1
            elif bb < rb:
                betting_blend_wins += 1

    print(f"  Brier score (pts, ast): Raw wins {betting_raw_wins}, Blend wins {betting_blend_wins}")
    print(f"  pts Brier: raw={raw_brier.get('pts', 'N/A'):.4f} vs blend={blend_brier.get('pts', 'N/A'):.4f}")
    print(f"  ast Brier: raw={raw_brier.get('ast', 'N/A'):.4f} vs blend={blend_brier.get('ast', 'N/A'):.4f}")
    print()


if __name__ == "__main__":
    main()
