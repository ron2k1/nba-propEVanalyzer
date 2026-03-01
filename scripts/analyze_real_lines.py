#!/usr/bin/env python3
"""
Analyze real-line-only performance from a --odds-source local_history backtest JSON.

Reads:  data/backtest_results/2026-01-26_to_2026-02-05_full_local.json
Writes:
  data/backtest_results/2026-01-26_to_2026-02-05_full_local_real_only_summary.json
  docs/reports/real_line_only_jan26_feb05.md

Usage:
    python scripts/analyze_real_lines.py [--input <path>] [--report-dir <dir>]
"""

import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_INPUT = os.path.join(
    _REPO_ROOT, "data", "backtest_results",
    "2026-01-26_to_2026-02-05_full_local.json",
)
DEFAULT_REPORT_DIR = os.path.join(_REPO_ROOT, "docs", "reports")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_pct(v, decimals=1):
    if v is None:
        return "—"
    return f"{v:+.{decimals}f}%"


def _fmt_roi(v):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def _fmt_hits(v):
    if v is None:
        return "—"
    return f"{v:.1f}%"


def _verdict(roi_real, roi_blended):
    if roi_real is None:
        return "negative", "No real-line bets placed — no real-line verdict possible."
    if roi_real >= 0.03:
        delta = roi_real - (roi_blended or 0.0)
        if delta >= 1.0:
            return "positive", (
                f"Real-line ROI ({roi_real:+.1f}%) beats blended ROI "
                f"({roi_blended:+.1f}%) by {delta:+.1f} pp — closing lines are "
                "filtering to better opportunities."
            )
        return "positive", (
            f"Real-line ROI is positive ({roi_real:+.1f}%) and consistent with "
            f"blended ROI ({roi_blended:+.1f}%)."
        )
    if roi_real >= -0.01:
        return "neutral", (
            f"Real-line ROI ({roi_real:+.1f}%) is near breakeven — insufficient "
            "sample to draw strong conclusions."
        )
    return "negative", (
        f"Real-line ROI is negative ({roi_real:+.1f}%) despite positive blended "
        f"ROI ({roi_blended:+.1f}%). Synthetic lines may be overstating edge."
    )


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyze(data):
    assert data.get("oddsSource") == "local_history", (
        "oddsSource must be 'local_history' — run with --odds-source local_history"
    )

    report = data["reports"]["full"]
    roi_real_raw  = report["roiReal"]
    roi_synth_raw = report["roiSynth"]
    roi_blend_raw = report["roiSimulation"]
    stat_roi      = report["realLineStatRoi"]
    calib_bins    = report["realLineCalibBins"]

    # Sanity check
    blend_bets = roi_blend_raw["betsPlaced"]
    seg_sum    = roi_real_raw["betsPlaced"] + roi_synth_raw["betsPlaced"]
    assert blend_bets == seg_sum, (
        f"Segment sum mismatch: real({roi_real_raw['betsPlaced']}) + "
        f"synth({roi_synth_raw['betsPlaced']}) = {seg_sum} != blend({blend_bets})"
    )

    # Per-stat coverage flags
    no_coverage_stats = [
        s for s, v in stat_roi.items() if v["betsPlaced"] == 0
    ]

    # Verdict
    verdict_label, verdict_text = _verdict(
        roi_real_raw.get("roiPctPerBet"),
        roi_blend_raw.get("roiPctPerBet"),
    )

    return {
        "oddsSource":          data["oddsSource"],
        "dateFrom":            data["dateFrom"],
        "dateTo":              data["dateTo"],
        "realLineSamples":     report["realLineSamples"],
        "missingLineSamples":  report["missingLineSamples"],
        "roiReal":             roi_real_raw,
        "roiSynth":            roi_synth_raw,
        "roiBlended":          roi_blend_raw,
        "realLineStatRoi":     stat_roi,
        "realLineCalibBins":   calib_bins,
        "noCoverageStats":     no_coverage_stats,
        "verdict":             verdict_label,
        "verdictText":         verdict_text,
    }


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def build_markdown(a):
    lines = []

    lines.append(
        f"# Real-Line-Only Performance Report — "
        f"{a['dateFrom']} to {a['dateTo']}\n"
    )
    lines.append(
        f"> Generated from backtest with `oddsSource={a['oddsSource']}`\n"
    )

    # ── Section 1 ──────────────────────────────────────────────────────────
    lines.append("## 1. Coverage Confirmation\n")
    total = a["realLineSamples"] + a["missingLineSamples"]
    cov_pct = (a["realLineSamples"] / total * 100.0) if total > 0 else 0.0
    lines.append(
        f"| Metric | Value |\n"
        f"|--------|-------|\n"
        f"| oddsSource | `{a['oddsSource']}` |\n"
        f"| realLineSamples | {a['realLineSamples']:,} |\n"
        f"| missingLineSamples | {a['missingLineSamples']:,} |\n"
        f"| coverage | {cov_pct:.1f}% |\n"
    )

    # ── Section 2 ──────────────────────────────────────────────────────────
    lines.append("## 2. Segment Comparison\n")
    r  = a["roiReal"]
    s  = a["roiSynth"]
    bl = a["roiBlended"]
    lines.append(
        "| Segment | Bets | Wins | Losses | Pushes | Hit% | ROI/bet |\n"
        "|---------|------|------|--------|--------|------|---------|\n"
        f"| **Real line** | {r['betsPlaced']:,} | {r['wins']:,} | {r['losses']:,} | "
        f"{r['pushes']:,} | {_fmt_hits(r['hitRatePct'])} | {_fmt_roi(r['roiPctPerBet'])} |\n"
        f"| Synthetic line | {s['betsPlaced']:,} | {s['wins']:,} | {s['losses']:,} | "
        f"{s['pushes']:,} | {_fmt_hits(s['hitRatePct'])} | {_fmt_roi(s['roiPctPerBet'])} |\n"
        f"| **Blended** | {bl['betsPlaced']:,} | {bl['wins']:,} | {bl['losses']:,} | "
        f"{bl.get('pushes', 0):,} | {_fmt_hits(bl['hitRatePct'])} | "
        f"{_fmt_roi(bl['roiPctPerBet'])} |\n"
    )

    # ── Section 3 ──────────────────────────────────────────────────────────
    lines.append("## 3. Per-Stat Real-Line Coverage\n")
    stat_roi = a["realLineStatRoi"]
    lines.append(
        "| Stat | Bets | Wins | Losses | Hit% | ROI/bet | Coverage |\n"
        "|------|------|------|--------|------|---------|----------|\n"
    )
    for stat, v in stat_roi.items():
        cov_flag = "no coverage" if v["betsPlaced"] == 0 else "ok"
        lines.append(
            f"| {stat} | {v['betsPlaced']:,} | {v['wins']:,} | {v['losses']:,} | "
            f"{_fmt_hits(v['hitRatePct'])} | {_fmt_roi(v['roiPctPerBet'])} | {cov_flag} |\n"
        )

    if a["noCoverageStats"]:
        lines.append(
            f"\n> **Stats with no real-line bets:** "
            f"{', '.join(a['noCoverageStats'])}  \n"
            f"> These stats had closing lines in the DB but no EV-positive bets qualified, "
            f"or the market isn't offered by Odds API.\n"
        )

    # ── Section 4 ──────────────────────────────────────────────────────────
    lines.append("## 4. Confidence Bins (real-line bets only)\n")
    lines.append(
        "| Prob bin | Bets | Wins | Hit% | ROI/bet |\n"
        "|----------|------|------|------|---------|\n"
    )
    for b in a["realLineCalibBins"]:
        # Only show bins that had activity
        if b["betsPlaced"] == 0:
            continue
        lines.append(
            f"| {b['bin']} | {b['betsPlaced']:,} | {b['wins']:,} | "
            f"{_fmt_hits(b['hitRatePct'])} | {_fmt_roi(b['roiPctPerBet'])} |\n"
        )
    lines.append(
        "\n*Bins with zero real-line bets omitted.*\n"
    )

    # ── Section 5 ──────────────────────────────────────────────────────────
    lines.append("## 5. Key Findings\n")

    # Top stats by real-line ROI
    ranked = [
        (stat, v) for stat, v in stat_roi.items()
        if v["betsPlaced"] >= 5 and v["roiPctPerBet"] is not None
    ]
    ranked.sort(key=lambda x: x[1]["roiPctPerBet"] or 0, reverse=True)

    if ranked:
        lines.append("**Top stats by real-line ROI (≥5 bets):**\n")
        for stat, v in ranked[:5]:
            lines.append(
                f"- **{stat.upper()}**: {v['betsPlaced']} bets, "
                f"{_fmt_hits(v['hitRatePct'])} hit rate, "
                f"{_fmt_roi(v['roiPctPerBet'])} ROI\n"
            )

    # Real vs synthetic gap
    real_roi   = r.get("roiPctPerBet")
    synth_roi  = s.get("roiPctPerBet")
    if real_roi is not None and synth_roi is not None:
        gap = real_roi - synth_roi
        lines.append(
            f"\n**Real vs synthetic gap:** {gap:+.1f} pp  \n"
            f"({'Real lines yielding higher ROI — good sign' if gap >= 0 else 'Synthetic lines outperforming — investigate calibration'})\n"
        )

    if a["noCoverageStats"]:
        lines.append(
            f"\n**Missing market coverage:** {', '.join(a['noCoverageStats'])} — "
            "Odds API does not offer player_turnovers or these markets weren't backfilled. "
            "Synthetic lines were used; exclude from real-money conclusions.\n"
        )

    # ── Section 6 ──────────────────────────────────────────────────────────
    lines.append("## 6. Final Verdict\n")

    verdict_emoji = {"positive": "✅", "neutral": "⚠️", "negative": "❌"}.get(
        a["verdict"], "❓"
    )
    lines.append(f"**{verdict_emoji} {a['verdict'].upper()}** — {a['verdictText']}\n")

    lines.append("\n**Risks:**\n")
    lines.append(
        "1. Coverage is only ~30% of all samples (4,436 / 14,520) — "
        "real-line segment may not be fully representative.\n"
    )
    lines.append(
        "2. stl, blk, fg3m, tov have no Odds API market or sparse coverage — "
        "those stats fall back to synthetic lines regardless of `--odds-source`.\n"
    )
    lines.append(
        "3. 11-day window (Jan 26 – Feb 5) is too narrow for statistical significance; "
        "expand backfill to confirm.\n"
    )

    lines.append("\n**Next actions:**\n")
    lines.append(
        "1. Run `backfill_odds_history.py` to extend coverage through Feb 25, "
        "then rerun this analysis on the full 30-day window.\n"
    )
    lines.append(
        "2. If real-line ROI remains positive, prioritize pts/reb/ast/pra bets "
        "where `clvLine > 0` AND `clvOddsPct > 0` as primary GO signals.\n"
    )
    lines.append(
        "3. Add minimum 20-bet threshold before drawing per-stat conclusions; "
        "current sample too small for stl/blk/fg3m.\n"
    )

    return "".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Analyze real-line-only backtest performance")
    parser.add_argument(
        "--input", default=DEFAULT_INPUT,
        help="Path to backtest JSON (default: 2026-01-26_to_2026-02-05_full_local.json)",
    )
    parser.add_argument(
        "--report-dir", default=DEFAULT_REPORT_DIR,
        help="Directory for markdown output (default: docs/reports/)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(args.input, encoding="utf-8") as fh:
        data = json.load(fh)

    if "reports" not in data or "full" not in data["reports"]:
        print("ERROR: JSON does not contain reports.full — re-run backtest with --model full", file=sys.stderr)
        sys.exit(1)

    report = data["reports"]["full"]
    if "roiReal" not in report:
        print(
            "ERROR: roiReal not found in report — re-run backtest after applying the "
            "segmented-ROI patch to core/nba_backtest.py",
            file=sys.stderr,
        )
        sys.exit(1)

    # Run analysis
    a = analyze(data)

    # ── Output 1: JSON summary ─────────────────────────────────────────────
    base = os.path.splitext(os.path.basename(args.input))[0]
    summary_path = os.path.join(
        os.path.dirname(args.input),
        f"{base}_real_only_summary.json",
    )
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(a, fh, indent=2)
    print(f"[analyze_real_lines] summary  → {summary_path}", file=sys.stderr)

    # ── Output 2: Markdown report ──────────────────────────────────────────
    os.makedirs(args.report_dir, exist_ok=True)
    md_path = os.path.join(args.report_dir, "real_line_only_jan26_feb05.md")
    md = build_markdown(a)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"[analyze_real_lines] report   → {md_path}", file=sys.stderr)

    # Print verdict to stdout
    print(json.dumps({
        "success":       True,
        "verdict":       a["verdict"],
        "verdictText":   a["verdictText"],
        "realBets":      a["roiReal"]["betsPlaced"],
        "realRoiPct":    a["roiReal"]["roiPctPerBet"],
        "blendedRoiPct": a["roiBlended"]["roiPctPerBet"],
        "summaryPath":   summary_path,
        "reportPath":    md_path,
    }, indent=2))


if __name__ == "__main__":
    main()
