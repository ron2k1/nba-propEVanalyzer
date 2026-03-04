#!/usr/bin/env python3
"""
2x2 Factorial Decomposition: Blend vs Gates ROI Isolation.

Usage:
    python scripts/compare_factorial.py --a <A.json> --b <B.json> --c <C.json> --d <D.json>

Variants:
    A = no-blend + 4-condition (--match-live --no-blend --no-gates)
    B = blend    + 4-condition (--match-live --no-gates)
    C = no-blend + 10-gate    (--match-live --no-blend)
    D = blend    + 10-gate    (--match-live)  [existing]

Factorial math:
    Blend effect  = mean(B,D) - mean(A,C)
    Gate effect   = mean(C,D) - mean(A,B)
    Interaction   = D - C - B + A
    Check: A + blend + gate + interaction = D
"""

import argparse
import json
import sys
from datetime import datetime


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


def _roi(rpt):
    """Extract real-line ROI% from report, fall back to simulation."""
    real = rpt.get("roiReal", {})
    if real.get("roiPctPerBet") is not None:
        return real["roiPctPerBet"]
    sim = rpt.get("roiSimulation", {})
    return sim.get("roiPctPerBet")


def _hit(rpt):
    real = rpt.get("roiReal", {})
    if real.get("hitRatePct") is not None:
        return real["hitRatePct"]
    sim = rpt.get("roiSimulation", {})
    return sim.get("hitRatePct")


def _signals(rpt):
    real = rpt.get("roiReal", {})
    if real.get("betsPlaced") is not None:
        return real["betsPlaced"]
    sim = rpt.get("roiSimulation", {})
    return sim.get("betsPlaced", 0)


def _fmt(v, suffix="%"):
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}{suffix}"


def factorial_decompose(a_roi, b_roi, c_roi, d_roi):
    blend_effect = ((b_roi + d_roi) / 2) - ((a_roi + c_roi) / 2)
    gate_effect = ((c_roi + d_roi) / 2) - ((a_roi + b_roi) / 2)
    interaction = d_roi - c_roi - b_roi + a_roi
    return {
        "blend_effect": blend_effect,
        "gate_effect": gate_effect,
        "interaction": interaction,
    }


def main():
    parser = argparse.ArgumentParser(description="2x2 Factorial: Blend vs Gates")
    parser.add_argument("--a", required=True, help="Variant A: no-blend + 4-condition")
    parser.add_argument("--b", required=True, help="Variant B: blend + 4-condition")
    parser.add_argument("--c", required=True, help="Variant C: no-blend + 10-gate")
    parser.add_argument("--d", required=True, help="Variant D: blend + 10-gate")
    args = parser.parse_args()

    data = {
        "A": _load(args.a),
        "B": _load(args.b),
        "C": _load(args.c),
        "D": _load(args.d),
    }
    rpts = {k: _rpt(v) for k, v in data.items()}

    print("=" * 72)
    print("FACTORIAL DECOMPOSITION: BLEND vs GATES")
    print(f"  Date range: {data['A'].get('dateFrom')} -> {data['A'].get('dateTo')}")
    print(f"  Generated:  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 72)
    print()

    # ── Section 1: Variant Summary ──
    labels = {
        "A": ("Off", "4-cond"),
        "B": ("On", "4-cond"),
        "C": ("Off", "10-gate"),
        "D": ("On", "10-gate"),
    }
    rows = []
    for v in "ABCD":
        r = rpts[v]
        blend, gates = labels[v]
        rows.append((v, blend, gates, _signals(r), _fmt(_roi(r)), _fmt(_hit(r))))

    print("VARIANT SUMMARY")
    _print_table(["Variant", "Blend", "Gates", "Signals", "ROI%", "Hit%"], rows)

    # ── Section 2: Factorial Decomposition ──
    rois = {v: _roi(rpts[v]) for v in "ABCD"}
    if any(rois[v] is None for v in "ABCD"):
        print("ERROR: Cannot compute factorial — missing ROI in one or more variants.")
        sys.exit(1)

    fx = factorial_decompose(rois["A"], rois["B"], rois["C"], rois["D"])

    mean_ac = (rois["A"] + rois["C"]) / 2
    mean_bd = (rois["B"] + rois["D"]) / 2
    mean_ab = (rois["A"] + rois["B"]) / 2
    mean_cd = (rois["C"] + rois["D"]) / 2

    print("FACTORIAL DECOMPOSITION (2x2)")
    print(f"{'':20s}  {'Gates=4-cond':>14s}  {'Gates=10-gate':>14s}  {'Row Mean':>10s}")
    print(f"{'Blend=Off':20s}  {'A: ' + _fmt(rois['A']):>14s}  {'C: ' + _fmt(rois['C']):>14s}  {_fmt(mean_ac):>10s}")
    print(f"{'Blend=On':20s}  {'B: ' + _fmt(rois['B']):>14s}  {'D: ' + _fmt(rois['D']):>14s}  {_fmt(mean_bd):>10s}")
    print(f"{'Column Mean':20s}  {_fmt(mean_ab):>14s}  {_fmt(mean_cd):>14s}")
    print()
    print(f"  Blend effect  = mean(B,D) - mean(A,C) = {_fmt(fx['blend_effect'])} pp")
    print(f"  Gate effect   = mean(C,D) - mean(A,B) = {_fmt(fx['gate_effect'])} pp")
    print(f"  Interaction   = D - C - B + A         = {_fmt(fx['interaction'])} pp")
    check = rois["A"] + fx["blend_effect"] + fx["gate_effect"] + fx["interaction"]
    print(f"  Check: A {_fmt(fx['blend_effect'])} {_fmt(fx['gate_effect'])} {_fmt(fx['interaction'])} = {_fmt(check)} (should = D: {_fmt(rois['D'])})")
    print()

    # ── Section 3: Per-Stat Breakdown ──
    stats = ["pts", "reb", "ast", "fg3m", "pra", "stl", "blk", "tov"]
    stat_rows = []
    for s in stats:
        has_data = False
        row = [s]
        for v in "ABCD":
            sr = rpts[v].get("realLineStatRoi", {}).get(s, {})
            n = sr.get("betsPlaced", 0)
            roi_val = sr.get("roiPctPerBet")
            if n > 0:
                has_data = True
            row.append(f"{n}")
            row.append(_fmt(roi_val) if roi_val is not None else "—")
        if has_data:
            stat_rows.append(tuple(row))

    if stat_rows:
        print("PER-STAT BREAKDOWN (real-line bets)")
        _print_table(
            ["Stat", "A #", "A ROI%", "B #", "B ROI%", "C #", "C ROI%", "D #", "D ROI%"],
            stat_rows,
        )

    # ── Section 4: Per-Bin Breakdown ──
    bin_rows = []
    for i in range(10):
        has_data = False
        bin_label = f"{i*10}-{(i+1)*10}%"
        row = [bin_label]
        for v in "ABCD":
            bins = rpts[v].get("realLineCalibBins", [])
            bb = bins[i] if i < len(bins) else {}
            n = bb.get("betsPlaced", 0)
            roi_val = bb.get("roiPctPerBet")
            if n > 0:
                has_data = True
            row.append(f"{n}")
            row.append(_fmt(roi_val) if roi_val is not None else "—")
        if has_data:
            bin_rows.append(tuple(row))

    if bin_rows:
        print("PER-BIN BREAKDOWN (real-line bets)")
        _print_table(
            ["Bin", "A #", "A ROI%", "B #", "B ROI%", "C #", "C ROI%", "D #", "D ROI%"],
            bin_rows,
        )

    # ── Section 5: Gate Rejection Analysis (C and D only) ──
    for v in ("C", "D"):
        rej = rpts[v].get("matchLiveRejections", {})
        if rej:
            label = labels[v]
            print(f"GATE REJECTIONS — Variant {v} (blend={'On' if label[0] == 'On' else 'Off'}, {label[1]})")
            rej_rows = sorted(rej.items(), key=lambda x: -x[1])
            _print_table(["Reason", "Count"], rej_rows)

    # ── Section 6: Accounting Check ──
    print("ACCOUNTING CHECK")
    ok = True
    for v in "ABCD":
        r = rpts[v]
        total_eval = r.get("matchLiveTotalEvaluated", 0)
        rej = sum(r.get("matchLiveRejections", {}).values())
        sim = r.get("roiSimulation", {})
        passed = sim.get("betsPlaced", 0)
        if total_eval > 0:
            gap = total_eval - rej - passed
            if gap >= 0:
                print(f"  {v}: {rej} rejected + {passed} passed + {gap} neg-EV = {total_eval} total  OK")
            else:
                print(f"  {v}: ACCOUNTING ERROR  {rej} rej + {passed} passed = {rej + passed} > {total_eval}")
                ok = False
        else:
            print(f"  {v}: No matchLiveTotalEvaluated data")

    if not ok:
        print("\nACCOUNTING FAILED")
        sys.exit(1)

    print()
    print("Done.")


if __name__ == "__main__":
    main()
