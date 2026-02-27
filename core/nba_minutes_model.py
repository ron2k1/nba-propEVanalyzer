#!/usr/bin/env python3
"""
Minutes model: computes a rich minutes projection with multiplier, confidence, and
reasoning tags.

Called from compute_projection() (nba_prep_projection.py) after _project_minutes()
to apply additional signals that _project_minutes() doesn't capture:
  - Short-term streak momentum (last 3 games vs avg10 baseline)
  - Volatility dampening (high-CV players → conservative multiplier)
  - Trend direction tagging (avg5 vs avg10)
  - B2B confidence reduction
  - Sample-size confidence

The minutesMultiplier is applied multiplicatively to _project_minutes() output.
Hard bounds: 0.85–1.15 to avoid large swings from the base projection.

External callers (e.g., injury_monitor, CLI) can call compute_minutes_multiplier()
directly to get a multiplier for a specific situation.

Evaluation helpers (minutes_calibration_bins) support the minutes_eval CLI command.
"""

import statistics

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

_MULTIPLIER_MIN    = 0.85    # floor on model-computed multiplier
_MULTIPLIER_MAX    = 1.15    # ceiling on model-computed multiplier
_VOLATILITY_HIGH   = 0.28    # CV > this → high_volatility (dampen trend)
_VOLATILITY_LOW    = 0.10    # CV < this → low_volatility (boost confidence)
_STREAK_N          = 3       # games to check for monotonic streak
_STREAK_THRESHOLD  = 0.03    # % above/below avg10 to count as streak direction
_STREAK_BOOST      = 1.04    # multiplier boost for confirmed up-streak
_STREAK_REDUCE     = 0.96    # multiplier reduction for confirmed down-streak
_TREND_THRESHOLD   = 0.08    # avg5 vs avg10 % delta to tag as trending


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_minutes_multiplier(
    rolling: dict,
    logs: list,
    is_b2b: bool = False,
    splits: dict = None,
) -> dict:
    """
    Compute a multiplier and confidence score for minutes projection.

    Inputs come directly from compute_projection()'s already-fetched data
    — no additional API calls are made.

    Parameters
    ----------
    rolling : dict   rolling stats dict from get_player_game_log (min_avg5, etc.)
    logs    : list   game-log list, most-recent-first
    is_b2b  : bool   True if this is a back-to-back game
    splits  : dict   player splits dict (currently used only for B2B reasoning tag)

    Returns
    -------
    {
      "multiplier":        float,       # 0.85–1.15, apply ON TOP of _project_minutes
      "minutesConfidence": float,       # 0.10–0.95
      "minutesReasoning":  list[str],   # machine-readable tags
      "last5Avg":          float,
      "last10Avg":         float,
      "seasonAvg":         float,
      "volatility":        float,       # coefficient of variation (stdev / mean)
    }
    """
    reasoning   = []
    multiplier  = 1.0
    confidence  = 0.70   # baseline

    avg5   = float(rolling.get("min_avg5",       0) or 0)
    avg10  = float(rolling.get("min_avg10",      0) or 0)
    avg_s  = float(rolling.get("min_avg_season", 0) or 0)
    stdev  = float(rolling.get("min_stdev",      0) or 0)

    # Coefficient of variation: primary measure of minutes consistency
    cv = stdev / avg_s if avg_s > 0 else 1.0

    # ------------------------------------------------------------------
    # Signal 1 — Volatility dampening
    # Does NOT duplicate _project_minutes() trend; this is pure risk mgmt.
    # ------------------------------------------------------------------
    if cv > _VOLATILITY_HIGH:
        # High variance: pull multiplier 40% closer to neutral
        multiplier  = 1.0 + (multiplier - 1.0) * 0.60
        confidence -= 0.12
        reasoning.append("high_volatility")
    elif cv < _VOLATILITY_LOW:
        confidence += 0.08
        reasoning.append("low_volatility")

    # ------------------------------------------------------------------
    # Signal 2 — Last-N streak direction (vs avg10, not avg5)
    # _project_minutes() already uses avg5 for trend; this checks avg10.
    # ------------------------------------------------------------------
    recent_mins = [float(g.get("min", 0) or 0) for g in (logs or [])[:_STREAK_N]]
    if len(recent_mins) >= _STREAK_N and avg10 > 0:
        threshold_up   = avg10 * (1 + _STREAK_THRESHOLD)
        threshold_down = avg10 * (1 - _STREAK_THRESHOLD)
        all_above = all(v > threshold_up   for v in recent_mins)
        all_below = all(v < threshold_down for v in recent_mins)
        if all_above:
            multiplier = min(_MULTIPLIER_MAX, multiplier * _STREAK_BOOST)
            reasoning.append("streak_up_3g")
        elif all_below:
            multiplier = max(_MULTIPLIER_MIN, multiplier * _STREAK_REDUCE)
            reasoning.append("streak_down_3g")
        else:
            reasoning.append("no_streak")

    # ------------------------------------------------------------------
    # Signal 3 — Trend direction tag (avg5 vs avg10)
    # Only a tag; multiplier already moved in signals 1 & 2.
    # ------------------------------------------------------------------
    if avg5 > 0 and avg10 > 0:
        trend_pct = (avg5 - avg10) / avg10
        if trend_pct > _TREND_THRESHOLD:
            reasoning.append("trending_up")
        elif trend_pct < -_TREND_THRESHOLD:
            reasoning.append("trending_down")
        else:
            reasoning.append("stable")

    # ------------------------------------------------------------------
    # Signal 4 — B2B confidence reduction
    # _project_minutes() already applies a quantitative rest_adj; this
    # records the tag and nudges confidence down.
    # ------------------------------------------------------------------
    if is_b2b:
        confidence -= 0.06
        reasoning.append("b2b")

    # ------------------------------------------------------------------
    # Signal 5 — Sample size confidence
    # ------------------------------------------------------------------
    n = len(logs or [])
    if n < 5:
        confidence -= 0.15
        reasoning.append("small_sample")
    elif n >= 15:
        confidence += 0.05
        reasoning.append("large_sample")

    # ------------------------------------------------------------------
    # Bounds
    # ------------------------------------------------------------------
    multiplier = max(_MULTIPLIER_MIN, min(_MULTIPLIER_MAX, multiplier))
    confidence = max(0.10, min(0.95, confidence))

    return {
        "multiplier":        round(multiplier, 4),
        "minutesConfidence": round(confidence, 3),
        "minutesReasoning":  reasoning,
        "last5Avg":          round(avg5,  2),
        "last10Avg":         round(avg10, 2),
        "seasonAvg":         round(avg_s, 2),
        "volatility":        round(cv,    4),
    }


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------

def minutes_calibration_bins(paired_list: list) -> dict:
    """
    Compute calibration of minutes projections in projected-minutes buckets.

    Parameters
    ----------
    paired_list : list of (projected_mins: float, actual_mins: float) tuples

    Returns
    -------
    {mae, bias, sampleCount, calibrationBuckets}
    """
    if not paired_list:
        return {"mae": None, "bias": None, "sampleCount": 0, "calibrationBuckets": []}

    errors = [abs(p - a) for p, a in paired_list]
    biases = [p - a       for p, a in paired_list]

    mae  = round(statistics.mean(errors), 3)
    bias = round(statistics.mean(biases), 3)

    # Buckets by projected minutes range
    buckets = {}
    for p, a in paired_list:
        if p < 15:
            bk = "0-15"
        elif p < 25:
            bk = "15-25"
        elif p < 35:
            bk = "25-35"
        else:
            bk = "35+"
        if bk not in buckets:
            buckets[bk] = {
                "count": 0, "sumErr": 0.0, "sumBias": 0.0,
                "sumProjected": 0.0, "sumActual": 0.0,
            }
        buckets[bk]["count"]        += 1
        buckets[bk]["sumErr"]        += abs(p - a)
        buckets[bk]["sumBias"]       += (p - a)
        buckets[bk]["sumProjected"]  += p
        buckets[bk]["sumActual"]     += a

    cal_out = []
    for bk_name in ("0-15", "15-25", "25-35", "35+"):
        bk = buckets.get(bk_name)
        if not bk or bk["count"] == 0:
            continue
        c = bk["count"]
        cal_out.append({
            "bucket":       bk_name,
            "count":        c,
            "mae":          round(bk["sumErr"]       / c, 2),
            "bias":         round(bk["sumBias"]      / c, 2),
            "avgProjected": round(bk["sumProjected"] / c, 2),
            "avgActual":    round(bk["sumActual"]     / c, 2),
        })

    return {
        "mae":                mae,
        "bias":               bias,
        "sampleCount":        len(paired_list),
        "calibrationBuckets": cal_out,
    }
