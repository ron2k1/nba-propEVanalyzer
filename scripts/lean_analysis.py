#!/usr/bin/env python3
"""Lean analysis: policy sensitivity report from --emit-all backtest JSONL.

Usage:
    python scripts/lean_analysis.py data/lean_bets.jsonl [--json]
"""

import json
import math
import sys
from collections import defaultdict
from datetime import datetime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_pct(num, denom):
    return round(num / denom * 100, 2) if denom else None


def _safe_roi(pnl, bets):
    return round(pnl / bets * 100, 2) if bets else None


def _roi_bucket(records):
    """Compute aggregate stats for a list of bet records."""
    bets = len(records)
    wins = sum(1 for r in records if r["outcome"] == "win")
    losses = sum(1 for r in records if r["outcome"] == "loss")
    pushes = sum(1 for r in records if r["outcome"] == "push")
    pnl = sum(r["pnl"] for r in records)
    real = [r for r in records if r.get("used_real_line")]
    real_bets = len(real)
    real_pnl = sum(r["pnl"] for r in real)
    return {
        "bets": bets,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "hitPct": _safe_pct(wins, bets),
        "roi": _safe_roi(pnl, bets),
        "pnl": round(pnl, 3),
        "realBets": real_bets,
        "realRoi": _safe_roi(real_pnl, real_bets),
        "realPnl": round(real_pnl, 3),
        "confidence": (
            "sufficient" if bets >= 30
            else "low" if bets >= 15
            else "insufficient"
        ),
    }


def _edge_bucket_label(edge):
    e = edge * 100
    if e < 2:
        return "0-2%"
    if e < 5:
        return "2-5%"
    if e < 10:
        return "5-10%"
    return "10%+"


def _confidence_warning(n):
    if n < 15:
        return " ** INSUFFICIENT (N<15) **"
    if n < 30:
        return " * LOW (N<30) *"
    return ""

# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def section_a_roi_by_category(bets):
    """A. ROI by Category."""
    policy_only = [b for b in bets if b["policy_pass"]]
    all_positive = bets  # all records have positive EV (emit_all gate)
    leans_only = [b for b in bets if b["has_positive_ev"] and not b["policy_pass"]]
    sub_threshold = [
        b for b in bets
        if b["has_positive_ev"] and not b["meets_threshold"] and not b["policy_pass"]
    ]
    return {
        "policy_only": _roi_bucket(policy_only),
        "all_positive_ev": _roi_bucket(all_positive),
        "leans_only": _roi_bucket(leans_only),
        "sub_threshold": _roi_bucket(sub_threshold),
    }


def section_b_roi_by_stat(bets):
    """B. ROI by Stat (all 8 stats)."""
    by_stat = defaultdict(list)
    for b in bets:
        by_stat[b["stat"]].append(b)
    result = {}
    for stat in sorted(by_stat):
        result[stat] = _roi_bucket(by_stat[stat])
    return result


def section_c_roi_by_bin(bets, blocked_bins=None):
    """C. ROI by Bin (all 10 bins)."""
    blocked_bins = blocked_bins or set()
    by_bin = defaultdict(list)
    for b in bets:
        by_bin[b["bin"]].append(b)
    result = {}
    for i in range(10):
        bucket = _roi_bucket(by_bin.get(i, []))
        bucket["blocked"] = i in blocked_bins
        result[str(i)] = bucket
    return result


def section_d_roi_by_edge(bets):
    """D. ROI by Edge Bucket."""
    by_edge = defaultdict(list)
    for b in bets:
        by_edge[_edge_bucket_label(b["edge"])].append(b)
    result = {}
    for label in ["0-2%", "2-5%", "5-10%", "10%+"]:
        result[label] = _roi_bucket(by_edge.get(label, []))
    return result


def _simulate_scenario(bets, policy_bets, *, stat_add=None, bin_unblock=None):
    """What-if: re-evaluate which leans would pass under a modified policy."""
    added = []
    for b in bets:
        if b["policy_pass"]:
            continue  # already in baseline
        if not b["has_positive_ev"] or not b["meets_threshold"]:
            continue
        detail = b["policy_detail"]
        stat_blocked = not detail["stat_in_whitelist"]
        bin_blocked = detail["bin_in_blocklist"]

        # Would this bet pass under the modified policy?
        stat_ok = detail["stat_in_whitelist"] or (
            stat_add is not None and b["stat"] in stat_add
        )
        bin_ok = not detail["bin_in_blocklist"] or (
            bin_unblock is not None and b["bin"] in bin_unblock
        )
        if stat_ok and bin_ok:
            added.append(b)

    combined = policy_bets + added
    baseline = _roi_bucket(policy_bets)
    scenario = _roi_bucket(combined)
    return {
        "bets_added": len(added),
        "new_total_bets": scenario["bets"],
        "new_roi": scenario["roi"],
        "roi_delta": (
            round(scenario["roi"] - baseline["roi"], 2)
            if scenario["roi"] is not None and baseline["roi"] is not None
            else None
        ),
        "new_hitPct": scenario["hitPct"],
        "new_pnl": scenario["pnl"],
        "real_line_roi": scenario["realRoi"],
        "real_bets": scenario["realBets"],
        "confidence": scenario["confidence"],
    }


def section_e_what_if(bets):
    """E. Policy 'What If' Scenarios."""
    policy_bets = [b for b in bets if b["policy_pass"]]
    scenarios = {}

    # Individual scenarios
    scenarios["+reb"] = _simulate_scenario(bets, policy_bets, stat_add={"reb"})
    scenarios["+pra"] = _simulate_scenario(bets, policy_bets, stat_add={"pra"})
    scenarios["unblock_bin6"] = _simulate_scenario(bets, policy_bets, bin_unblock={6})
    scenarios["unblock_bin7"] = _simulate_scenario(bets, policy_bets, bin_unblock={7})

    # Combined scenarios
    scenarios["+reb_AND_unblock_bin7"] = _simulate_scenario(
        bets, policy_bets, stat_add={"reb"}, bin_unblock={7}
    )
    scenarios["+pra_AND_+reb"] = _simulate_scenario(
        bets, policy_bets, stat_add={"pra", "reb"}
    )
    scenarios["+reb_AND_unblock_bin6"] = _simulate_scenario(
        bets, policy_bets, stat_add={"reb"}, bin_unblock={6}
    )
    scenarios["+pra_AND_unblock_bin7"] = _simulate_scenario(
        bets, policy_bets, stat_add={"pra"}, bin_unblock={7}
    )

    return scenarios


def section_f_sample_warnings(bets):
    """F. Sample Size Warnings — flag slices with N < 30."""
    warnings = []
    # By stat
    by_stat = defaultdict(list)
    for b in bets:
        by_stat[b["stat"]].append(b)
    for stat, recs in sorted(by_stat.items()):
        n = len(recs)
        if n < 30:
            warnings.append({
                "slice": f"stat={stat}",
                "n": n,
                "confidence": "low" if n >= 15 else "insufficient",
            })
    # By bin
    by_bin = defaultdict(list)
    for b in bets:
        by_bin[b["bin"]].append(b)
    for bin_idx in range(10):
        n = len(by_bin.get(bin_idx, []))
        if n < 30 and n > 0:
            warnings.append({
                "slice": f"bin={bin_idx}",
                "n": n,
                "confidence": "low" if n >= 15 else "insufficient",
            })
    return warnings


def section_g_time_series(bets):
    """G. Time-Series Stability — split into two halves."""
    if not bets:
        return {}
    dates = sorted(set(b["date"] for b in bets))
    mid = len(dates) // 2
    first_dates = set(dates[:mid])
    second_dates = set(dates[mid:])

    first = [b for b in bets if b["date"] in first_dates]
    second = [b for b in bets if b["date"] in second_dates]

    result = {
        "firstHalf": {
            "dateRange": f"{min(first_dates)} to {max(first_dates)}" if first_dates else "",
            "days": len(first_dates),
        },
        "secondHalf": {
            "dateRange": f"{min(second_dates)} to {max(second_dates)}" if second_dates else "",
            "days": len(second_dates),
        },
        "splits": {},
    }

    # By stat
    for stat in sorted(set(b["stat"] for b in bets)):
        f1 = [b for b in first if b["stat"] == stat]
        f2 = [b for b in second if b["stat"] == stat]
        r1 = _roi_bucket(f1)
        r2 = _roi_bucket(f2)
        trend = "insufficient_data"
        if r1["roi"] is not None and r2["roi"] is not None:
            delta = r2["roi"] - r1["roi"]
            if abs(delta) < 5:
                trend = "stable"
            elif delta > 0:
                trend = "improving"
            else:
                trend = "decaying"
        result["splits"][f"stat_{stat}"] = {
            "firstHalf": {"bets": r1["bets"], "hitPct": r1["hitPct"], "roi": r1["roi"]},
            "secondHalf": {"bets": r2["bets"], "hitPct": r2["hitPct"], "roi": r2["roi"]},
            "trend": trend,
        }

    # Policy vs leans
    for label, pred in [("policy", lambda b: b["policy_pass"]),
                        ("leans", lambda b: not b["policy_pass"])]:
        f1 = [b for b in first if pred(b)]
        f2 = [b for b in second if pred(b)]
        r1 = _roi_bucket(f1)
        r2 = _roi_bucket(f2)
        trend = "insufficient_data"
        if r1["roi"] is not None and r2["roi"] is not None:
            delta = r2["roi"] - r1["roi"]
            if abs(delta) < 5:
                trend = "stable"
            elif delta > 0:
                trend = "improving"
            else:
                trend = "decaying"
        result["splits"][label] = {
            "firstHalf": {"bets": r1["bets"], "hitPct": r1["hitPct"], "roi": r1["roi"]},
            "secondHalf": {"bets": r2["bets"], "hitPct": r2["hitPct"], "roi": r2["roi"]},
            "trend": trend,
        }

    return result


def section_h_overlap(bets):
    """H. Overlap Analysis — how much do leans overlap with policy picks."""
    policy = [b for b in bets if b["policy_pass"]]
    leans = [b for b in bets if not b["policy_pass"]]

    if not leans:
        return {"lean_count": 0, "overlap_pct_same_game": None,
                "overlap_pct_same_player": None, "diversification": "n/a"}

    # Same-game overlap: (date, player_id) combos in policy
    policy_games = set((b["date"], b["player_id"]) for b in policy)
    policy_players = set((b["date"], b["player_id"]) for b in policy)

    # For leans: how many land on same (date, player) as a policy pick?
    same_player = sum(
        1 for b in leans if (b["date"], b["player_id"]) in policy_players
    )
    # Same game-day overlap (date only)
    policy_dates = set(b["date"] for b in policy)
    same_gameday = sum(1 for b in leans if b["date"] in policy_dates)

    overlap_player_pct = _safe_pct(same_player, len(leans))
    overlap_gameday_pct = _safe_pct(same_gameday, len(leans))

    # Diversification: low overlap = more value from adding leans
    if overlap_player_pct is not None and overlap_player_pct < 30:
        diversification = "high"
    elif overlap_player_pct is not None and overlap_player_pct < 60:
        diversification = "medium"
    else:
        diversification = "low"

    return {
        "lean_count": len(leans),
        "policy_count": len(policy),
        "overlap_pct_same_player": overlap_player_pct,
        "overlap_pct_same_gameday": overlap_gameday_pct,
        "diversification": diversification,
    }


def section_j_olv(bets):
    """J. Opening Line Value — did the market move toward our model?"""
    with_olv = [b for b in bets if b.get("olv_favorable") is not None]
    if not with_olv:
        return {"betsWithOlv": 0, "note": "no opening line data available"}

    favorable = [b for b in with_olv if b["olv_favorable"]]
    unfavorable = [b for b in with_olv if not b["olv_favorable"]]

    # By category
    policy_olv = [b for b in with_olv if b["policy_pass"]]
    leans_olv = [b for b in with_olv if not b["policy_pass"]]

    def _olv_stats(records):
        fav = [r for r in records if r["olv_favorable"]]
        unfav = [r for r in records if not r["olv_favorable"]]
        movements = [abs(r["line_movement"]) for r in records if r.get("line_movement")]
        return {
            "bets": len(records),
            "favorable": len(fav),
            "unfavorable": len(unfav),
            "favorablePct": _safe_pct(len(fav), len(records)),
            "roiFavorable": _roi_bucket(fav) if fav else None,
            "roiUnfavorable": _roi_bucket(unfav) if unfav else None,
            "avgMovement": round(sum(movements) / len(movements), 2) if movements else None,
        }

    # By stat
    by_stat = defaultdict(list)
    for b in with_olv:
        by_stat[b["stat"]].append(b)

    return {
        "betsWithOlv": len(with_olv),
        "overall": _olv_stats(with_olv),
        "policy": _olv_stats(policy_olv),
        "leans": _olv_stats(leans_olv),
        "byStat": {stat: _olv_stats(recs) for stat, recs in sorted(by_stat.items())},
    }


def section_i_decision_matrix(bets, scenarios):
    """I. Machine-Readable Decision Matrix (JSON)."""
    policy_bets = [b for b in bets if b["policy_pass"]]
    baseline = _roi_bucket(policy_bets)

    recommendations = []
    for action, sc in sorted(scenarios.items(), key=lambda x: -(x[1].get("roi_delta") or -999)):
        recommendations.append({
            "action": action,
            "bets_added": sc["bets_added"],
            "new_total_bets": sc["new_total_bets"],
            "new_roi": sc["new_roi"],
            "roi_delta": sc["roi_delta"],
            "real_line_roi": sc["real_line_roi"],
            "confidence": sc["confidence"],
        })

    return {
        "baseline": {
            "bets": baseline["bets"],
            "roi": baseline["roi"],
            "hitPct": baseline["hitPct"],
            "realRoi": baseline["realRoi"],
        },
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# Pretty-print (text report)
# ---------------------------------------------------------------------------

def _print_table(title, rows, headers):
    """Print a simple ASCII table."""
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")
    widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=4)) + 2
              for i, h in enumerate(headers)]
    header_line = "".join(str(h).ljust(w) for h, w in zip(headers, widths))
    print(header_line)
    print("-" * sum(widths))
    for row in rows:
        print("".join(str(v).ljust(w) for v, w in zip(row, widths)))


def print_report(report):
    """Print human-readable report to stdout."""
    print("\n" + "=" * 70)
    print("  LEAN ANALYSIS — POLICY SENSITIVITY REPORT")
    print("=" * 70)

    # A. ROI by Category
    cat = report["roiByCategory"]
    headers = ["Category", "Bets", "Wins", "Hit%", "ROI%", "PnL", "Conf"]
    rows = []
    for label in ["policy_only", "all_positive_ev", "leans_only", "sub_threshold"]:
        c = cat[label]
        rows.append([
            label, c["bets"], c["wins"], c["hitPct"], c["roi"], c["pnl"],
            _confidence_warning(c["bets"]),
        ])
    _print_table("A. ROI by Category", rows, headers)

    # B. ROI by Stat
    stat_data = report["roiByStat"]
    headers = ["Stat", "Bets", "Wins", "Hit%", "ROI%", "PnL", "RealBets", "RealROI%"]
    rows = []
    for stat in sorted(stat_data):
        s = stat_data[stat]
        rows.append([
            stat, s["bets"], s["wins"], s["hitPct"], s["roi"], s["pnl"],
            s["realBets"], s["realRoi"],
        ])
    _print_table("B. ROI by Stat (all 8)", rows, headers)

    # C. ROI by Bin
    bin_data = report["roiByBin"]
    headers = ["Bin", "Bets", "Wins", "Hit%", "ROI%", "Blocked?", "Conf"]
    rows = []
    for i in range(10):
        b = bin_data[str(i)]
        blocked = "BLOCKED" if b["blocked"] else ""
        rows.append([
            f"{i*10}-{(i+1)*10}%", b["bets"], b["wins"], b["hitPct"],
            b["roi"], blocked, _confidence_warning(b["bets"]),
        ])
    _print_table("C. ROI by Prob Bin", rows, headers)

    # D. ROI by Edge
    edge_data = report["roiByEdge"]
    headers = ["Edge", "Bets", "Wins", "Hit%", "ROI%", "Conf"]
    rows = []
    for label in ["0-2%", "2-5%", "5-10%", "10%+"]:
        e = edge_data[label]
        rows.append([
            label, e["bets"], e["wins"], e["hitPct"], e["roi"],
            _confidence_warning(e["bets"]),
        ])
    _print_table("D. ROI by Edge Bucket", rows, headers)

    # E. What-If Scenarios
    scenarios = report["whatIfScenarios"]
    headers = ["Scenario", "Added", "Total", "NewROI%", "Delta", "RealROI%", "Conf"]
    rows = []
    for name in sorted(scenarios, key=lambda k: -(scenarios[k].get("roi_delta") or -999)):
        sc = scenarios[name]
        rows.append([
            name, sc["bets_added"], sc["new_total_bets"], sc["new_roi"],
            sc["roi_delta"], sc["real_line_roi"], sc["confidence"],
        ])
    _print_table("E. Policy What-If Scenarios", rows, headers)

    # F. Sample Warnings
    warnings = report["sampleWarnings"]
    if warnings:
        print(f"\n{'=' * 70}")
        print("  F. Sample Size Warnings")
        print(f"{'=' * 70}")
        for w in warnings:
            flag = "** INSUFFICIENT **" if w["confidence"] == "insufficient" else "* LOW *"
            print(f"  {w['slice']:20s}  N={w['n']:>4d}  {flag}")
    else:
        print("\n  F. No sample size warnings (all slices N >= 30)")

    # G. Time-Series Stability
    ts = report["timeSeries"]
    if ts and ts.get("splits"):
        print(f"\n{'=' * 70}")
        print(f"  G. Time-Series Stability")
        print(f"  First half:  {ts['firstHalf'].get('dateRange', '?')} ({ts['firstHalf'].get('days', 0)} days)")
        print(f"  Second half: {ts['secondHalf'].get('dateRange', '?')} ({ts['secondHalf'].get('days', 0)} days)")
        print(f"{'=' * 70}")
        headers = ["Split", "H1 Bets", "H1 Hit%", "H1 ROI%", "H2 Bets", "H2 Hit%", "H2 ROI%", "Trend"]
        rows = []
        for key in sorted(ts["splits"]):
            sp = ts["splits"][key]
            rows.append([
                key,
                sp["firstHalf"]["bets"], sp["firstHalf"]["hitPct"], sp["firstHalf"]["roi"],
                sp["secondHalf"]["bets"], sp["secondHalf"]["hitPct"], sp["secondHalf"]["roi"],
                sp["trend"],
            ])
        _print_table("", rows, headers)

    # H. Overlap
    ov = report["overlap"]
    print(f"\n{'=' * 70}")
    print("  H. Overlap Analysis")
    print(f"{'=' * 70}")
    print(f"  Policy bets:        {ov.get('policy_count', 0)}")
    print(f"  Lean bets:          {ov.get('lean_count', 0)}")
    print(f"  Same-player overlap: {ov.get('overlap_pct_same_player')}%")
    print(f"  Same-gameday overlap: {ov.get('overlap_pct_same_gameday')}%")
    print(f"  Diversification:    {ov.get('diversification')}")

    # I. Decision Matrix summary
    dm = report["decisionMatrix"]
    print(f"\n{'=' * 70}")
    print("  I. Decision Matrix")
    print(f"{'=' * 70}")
    bl = dm["baseline"]
    print(f"  Baseline: {bl['bets']} bets | {bl['hitPct']}% hit | {bl['roi']}% ROI | realROI={bl['realRoi']}%")
    print()
    for rec in dm["recommendations"][:5]:
        print(f"  {rec['action']:30s} +{rec['bets_added']} bets -> {rec['new_total_bets']} total | "
              f"ROI={rec['new_roi']}% (delta={rec['roi_delta']:+.2f}) | "
              f"realROI={rec['real_line_roi']}% | {rec['confidence']}")

    # J. OLV
    olv = report.get("olv", {})
    if olv.get("betsWithOlv", 0) > 0:
        print(f"\n{'=' * 70}")
        print("  J. Opening Line Value (OLV) — Market Agreement")
        print(f"{'=' * 70}")
        ov_all = olv.get("overall", {})
        print(f"  Bets with OLV data: {olv['betsWithOlv']}")
        print(f"  Favorable (market moved toward model): {ov_all.get('favorable', 0)} "
              f"({ov_all.get('favorablePct', 0)}%)")
        print(f"  Avg line movement: {ov_all.get('avgMovement', '?')} pts")
        fav_roi = ov_all.get("roiFavorable", {})
        unfav_roi = ov_all.get("roiUnfavorable", {})
        if fav_roi:
            print(f"  ROI when favorable:   {fav_roi.get('roi')}% on {fav_roi.get('bets')} bets")
        if unfav_roi:
            print(f"  ROI when unfavorable: {unfav_roi.get('roi')}% on {unfav_roi.get('bets')} bets")

        # By stat
        by_stat = olv.get("byStat", {})
        if by_stat:
            headers = ["Stat", "OLV Bets", "Fav%", "FavROI%", "UnfavROI%", "AvgMove"]
            rows = []
            for stat in sorted(by_stat):
                s = by_stat[stat]
                fav_r = (s.get("roiFavorable") or {}).get("roi")
                unfav_r = (s.get("roiUnfavorable") or {}).get("roi")
                rows.append([
                    stat, s["bets"], s["favorablePct"], fav_r, unfav_r,
                    s.get("avgMovement"),
                ])
            _print_table("  OLV by Stat", rows, headers)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_analysis(jsonl_path, output_json=False):
    """Load JSONL, run all sections, return report dict."""
    bets = []
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line_num, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"WARNING: skipping malformed line {line_num}: {exc}",
                      file=sys.stderr)
                continue
            bets.append(rec)

    if not bets:
        print("ERROR: no bet records found in", jsonl_path, file=sys.stderr)
        return {"error": "no records", "file": jsonl_path}

    # Infer blocked bins from records
    blocked_bins = set()
    for b in bets:
        detail = b.get("policy_detail", {})
        if detail.get("bin_in_blocklist") and detail.get("blocked_bin") is not None:
            blocked_bins.add(detail["blocked_bin"])

    print(f"Loaded {len(bets)} bet records from {jsonl_path}", file=sys.stderr)
    policy_count = sum(1 for b in bets if b.get("policy_pass"))
    lean_count = len(bets) - policy_count
    print(f"  policy_pass: {policy_count} | leans: {lean_count}", file=sys.stderr)

    scenarios = section_e_what_if(bets)

    report = {
        "file": jsonl_path,
        "totalRecords": len(bets),
        "policyPassCount": policy_count,
        "leanCount": lean_count,
        "roiByCategory": section_a_roi_by_category(bets),
        "roiByStat": section_b_roi_by_stat(bets),
        "roiByBin": section_c_roi_by_bin(bets, blocked_bins),
        "roiByEdge": section_d_roi_by_edge(bets),
        "whatIfScenarios": scenarios,
        "sampleWarnings": section_f_sample_warnings(bets),
        "timeSeries": section_g_time_series(bets),
        "overlap": section_h_overlap(bets),
        "decisionMatrix": section_i_decision_matrix(bets, scenarios),
        "olv": section_j_olv(bets),
    }

    if output_json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
        # Always emit JSON as the last line for machine parsing
        print()
        print(json.dumps(report))

    return report


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/lean_analysis.py <bets.jsonl> [--json]",
              file=sys.stderr)
        sys.exit(1)

    jsonl_path = sys.argv[1]
    output_json = "--json" in sys.argv

    run_analysis(jsonl_path, output_json=output_json)


if __name__ == "__main__":
    main()
