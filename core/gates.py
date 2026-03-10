#!/usr/bin/env python3
"""
Pure gate logic — no side effects, no DB, no logging.

Extracted from nba_decision_journal.py so backtest and live pipelines
can share a single source of truth for signal qualification.
"""

from . import policy_config as _pc

# ---------------------------------------------------------------------------
# Signal specification (frozen constant)
# ---------------------------------------------------------------------------

SIGNAL_SPEC = {
    "v1": {
        "eligible_stats":      set(_pc.ELIGIBLE_STATS),
        "min_edge":            _pc.MIN_EDGE,
        "min_edge_by_stat":    dict(_pc.MIN_EDGE_BY_STAT),
        "min_confidence":      _pc.MIN_CONFIDENCE,
        "blocked_prob_bins":   set(_pc.BLOCKED_PROB_BINS),
        "real_line_required_stats": set(_pc.REAL_LINE_REQUIRED_STATS),
        "paper_mode":          True,
        # Pinnacle confirmation gate (Phase 1a)
        "require_pinnacle":    True,
        "pinnacle_thresholds": dict(_pc.PINNACLE_THRESHOLDS),
        "pinnacle_min_no_vig_by_stat": dict(_pc.PINNACLE_MIN_NO_VIG_BY_STAT),
        # High-variance role-instability block (Phase 2b)
        "block_high_variance": True,
        # Intraday CLV: informational only — stored in context_json (Phase 1c)
        # Only set when ≥2 distinct timestamps exist for (player, stat, book, date)
        "min_intraday_clv_pct": 0,
        # reb: signal-eligible (for research/CLV tracking) but BETTING_POLICY blocks
        # betting on it. Signals are still journaled for calibration data collection.
        # Source tracking: all signals include context_json["source"] for ROI by source
        # Gap 8.8: minimum number of books posting this prop for market validation.
        # A prop offered by only 1 book may be a pricing error or test line — not consensus.
        # Absent (backtest / older result dicts): gate is skipped for backward compat.
        "min_books_offering": 2,
        # Gap 8.12: maximum allowed cross-book line stdev (diagnostic, not a blocker yet).
        # bookLineStdev=0 means all books quote identical lines (stale consensus risk).
        # Stored in context_json for future CLV correlation analysis.
        "max_book_line_dispersion": 0.75,
    }
}
CURRENT_SIGNAL_VERSION = "v1"


# ---------------------------------------------------------------------------
# Pure qualifier — takes data in, returns (pass, reason), no side effects
# ---------------------------------------------------------------------------

def _qualifies(prop_result: dict, stat: str, used_real_line=None) -> tuple:
    """
    Returns (qualifies, skip_reason). Pure function — no I/O, no DB, no logging.

    used_real_line: True/False if known; None means unknown (treated as False
    for real_line_required_stats check).
    """
    spec     = SIGNAL_SPEC[CURRENT_SIGNAL_VERSION]
    stat_key = str(stat or "").lower()
    if stat_key not in spec["eligible_stats"]:
        return False, f"stat_not_eligible:{stat}"
    # Real-line gate: skip if this stat requires a live Odds API line and we don't have one
    if stat_key in spec.get("real_line_required_stats", set()):
        if not used_real_line:
            return False, f"real_line_required:{stat_key}"
    ev    = (prop_result or {}).get("ev") or {}
    eo    = float((ev.get("over")  or {}).get("edge") or 0.0)
    eu    = float((ev.get("under") or {}).get("edge") or 0.0)
    prob_over = float(ev.get("probOver") or 0.0)
    conf  = max(prob_over, float(ev.get("probUnder") or 0.0))
    # Stat-specific minimum edge (falls back to global min_edge)
    min_edge = spec.get("min_edge_by_stat", {}).get(stat_key, spec["min_edge"])
    if max(eo, eu) < min_edge:
        return False, f"edge_too_low:{max(eo,eu):.4f}"
    if conf < spec["min_confidence"]:
        return False, f"confidence_too_low:{conf:.4f}"
    blocked = spec.get("blocked_prob_bins", set())
    if blocked:
        bin_idx = max(0, min(9, int(prob_over * 10)))
        if bin_idx in blocked:
            return False, f"blocked_prob_bin:{bin_idx}"
    # CLV gate: block strictly negative; CLV=0 (neutral) passes through
    x = prop_result.get("clvLine"); clv_line = float(x) if x is not None else None
    x = prop_result.get("clvOddsPct"); clv_odds = float(x) if x is not None else None
    if clv_line is not None and clv_odds is not None:
        if clv_line < 0 or clv_odds < 0:
            return False, f"clv_gate_failed:line={clv_line} odds={clv_odds}"
    # Injury-return gate: block first-game-back with severe minutes restriction (≤72%)
    # Handles both explicit-DNP tag "injury_return_g1:72pct"
    # and calendar-gap tag "injury_return_gap_10d_g1_cap_72pct"
    minutes_proj = (prop_result or {}).get("minutesProjection") or {}
    for tag in (minutes_proj.get("minutesReasoning") or []):
        pct = None
        if tag.startswith("injury_return_g1:"):
            try:
                pct = int(tag.split(":")[1].replace("pct", ""))
            except (IndexError, ValueError):
                pass
        elif "g1_cap_" in tag and tag.startswith("injury_return_"):
            try:
                pct = int(tag.split("g1_cap_")[1].replace("pct", ""))
            except (IndexError, ValueError):
                pass
        if pct is not None and pct <= 72:
            return False, f"injury_return_g1_blocked:{tag}"
    # Pinnacle confirmation gate (Phase 1a):
    # Use the recommended side's no-vig probability. If require_pinnacle is True
    # but no Pinnacle data was fetched (referenceBook absent), pass through — the
    # caller is responsible for fetching Pinnacle (backtests legitimately skip this).
    if spec.get("require_pinnacle"):
        ref = (prop_result or {}).get("referenceBook") or {}
        if ref:
            # Determine recommended side from edge magnitudes
            rec_side = "over" if eo >= eu else "under"
            no_vig_rec = ref.get("noVigOver") if rec_side == "over" else ref.get("noVigUnder")
            if no_vig_rec is None:
                return False, f"no_pinnacle_no_vig_{rec_side}"
            bin_idx = max(0, min(9, int(prob_over * 10)))
            # Per-stat threshold (falls back to global pinnacle_thresholds)
            _pinn_by_stat = spec.get("pinnacle_min_no_vig_by_stat", {})
            _pinn_global  = spec.get("pinnacle_thresholds", {})
            min_nv = _pinn_by_stat.get(stat_key) or _pinn_global.get(bin_idx)
            if min_nv is not None and float(no_vig_rec) < min_nv:
                return False, f"pinnacle_{rec_side}_too_low:{no_vig_rec:.3f}<{min_nv}"
        # No referenceBook → Pinnacle not fetched (backtest or caller omitted) → pass through
    # High-variance block (Phase 2b):
    # If last-5 stdev > 1.5× full-window stdev, role/usage is unstable.
    if spec.get("block_high_variance"):
        proj_data = (prop_result or {}).get("projection") or {}
        if proj_data.get("recentHighVariance") is True:
            return False, "recent_high_variance"
    # Gap 8.16: cross-book line dispersion — informational only, no block.
    # softLineBooks and bookLineStdev are stored in context_json by the caller
    # when present. max_book_line_dispersion=0.75 is a future threshold for
    # correlation analysis; dispersion alone is not a gating condition.
    # Gap 8.8: market depth gate.
    # Block if nBooksOffering is present and below min_books_offering.
    # If absent (backtest compat, older result dicts): skip check entirely.
    _n_books_raw = (prop_result or {}).get("nBooksOffering")
    if _n_books_raw is not None:
        _n_books = int(_n_books_raw)
        _min_books = int(spec.get("min_books_offering", 1))
        if _n_books > 0 and _n_books < _min_books:
            return False, f"only_one_book:{_n_books}"
    return True, ""
