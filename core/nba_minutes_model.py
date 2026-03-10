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
Dual-floor system: normal range 0.85–1.15 for model signals; absolute floor
0.50 allows injury caps below the normal range.

External callers (e.g., injury_monitor, CLI) can call compute_minutes_multiplier()
directly to get a multiplier for a specific situation.

Evaluation helpers (minutes_calibration_bins) support the minutes_eval CLI command.
"""

import statistics
from datetime import datetime

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

_MULTIPLIER_NORMAL_MIN  = 0.85    # floor for model signals (streak, trend)
_MULTIPLIER_ABSOLUTE_MIN = 0.50   # absolute floor — allows injury caps below normal range
_MULTIPLIER_MAX         = 1.15    # ceiling on model-computed multiplier
_VOLATILITY_HIGH   = 0.28    # CV > this → high_volatility (dampen trend)
_VOLATILITY_LOW    = 0.10    # CV < this → low_volatility (boost confidence)
_STREAK_N          = 3       # games to check for monotonic streak
_STREAK_THRESHOLD  = 0.03    # % above/below avg10 to count as streak direction
_STREAK_BOOST      = 1.04    # multiplier boost for confirmed up-streak
_STREAK_REDUCE     = 0.96    # multiplier reduction for confirmed down-streak
_TREND_THRESHOLD   = 0.08    # avg5 vs avg10 % delta to tag as trending


# ---------------------------------------------------------------------------
# Injury-return detection
# ---------------------------------------------------------------------------

_INJURY_CAP_TABLE = [
    # (min_dnps, min_games_since_return, max_games_since_return, cap)
    (6, 1, 1, 0.65),
    (3, 1, 1, 0.72),
    (1, 1, 1, 0.82),
    (1, 2, 2, 0.85),
    (1, 3, 3, 0.92),
]
_AVG_DAYS_PER_DNP = 2.4  # avg NBA schedule density
_LAYOFF_GAP_DAYS = 4      # gap between games >= this → treat as return from layoff (API omits DNPs)


def _parse_game_date(date_str: str):
    """Parse a game date string to a datetime.date. Returns None on failure.
    Handles NBA API format (e.g. 'FEB 26, 2026') by normalizing month to title case for %b.
    """
    s = (date_str or "").strip()
    if not s:
        return None
    # NBA API often returns "FEB 26, 2026"; %b is locale-sensitive and may need "Feb"
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, AttributeError):
            pass
    # Retry with month in title case for "MMM DD, YYYY" (e.g. FEB -> Feb)
    parts = s.split(None, 2)
    if len(parts) >= 3 and len(parts[0]) >= 3:
        normalized = parts[0].title() + " " + parts[1] + ", " + parts[2]
        try:
            return datetime.strptime(normalized, "%b %d, %Y").date()
        except (ValueError, AttributeError):
            pass
    return None


def detect_injury_return(logs: list, excluded_games: list) -> dict:
    """
    Detect return-from-injury by finding a DNP streak immediately before
    the most recent active game.

    Parameters
    ----------
    logs            : list  active game logs (most-recent-first), from get_player_game_log
    excluded_games  : list  DNP games, each {"gameDate": str, "gameId": str}

    Returns
    -------
    {
      "is_returning":       bool,
      "consecutive_dnps":   int,
      "approx_days_missed": float,
      "games_since_return": int,
      "cap_multiplier":     float,
      "reasoning":          str,
    }
    """
    _no_cap = {
        "is_returning": False, "consecutive_dnps": 0,
        "approx_days_missed": 0.0, "games_since_return": 0,
        "cap_multiplier": 1.0, "reasoning": "no_injury_cap",
    }

    if not logs:
        return _no_cap

    # Parse active game dates (most-recent-first assumed)
    active_dates = []
    for g in logs:
        d = _parse_game_date(g.get("gameDate", ""))
        if d:
            active_dates.append(d)

    if not active_dates:
        return _no_cap

    # Parse DNP dates
    dnp_dates = []
    for g in (excluded_games or []):
        d = _parse_game_date(g.get("gameDate", ""))
        if d:
            dnp_dates.append(d)

    most_recent_active = active_dates[0]

    # Count consecutive DNPs that all occurred AFTER the second-most-recent
    # active game (i.e., immediately before the most recent active game)
    second_active = active_dates[1] if len(active_dates) > 1 else None

    consecutive_dnps = sum(
        1 for d in dnp_dates
        if d > (second_active or most_recent_active)
        and d < most_recent_active
    ) if second_active is not None else sum(
        1 for d in dnp_dates if d < most_recent_active
    )

    if consecutive_dnps == 0:
        # NBA API omits DNP games — only games played appear. Use calendar gap as fallback.
        if len(active_dates) >= 2:
            gap_days = (most_recent_active - second_active).days
            if gap_days >= _LAYOFF_GAP_DAYS:
                estimated_dnps = max(1, round(gap_days / _AVG_DAYS_PER_DNP))
                # Count active games already played after the gap (comeback games
                # already in the log). The projected game is one more after those.
                games_played_after_gap = sum(1 for d in active_dates if d > second_active)
                games_since_return = games_played_after_gap + 1
                cap = 1.0
                for min_dnps, min_gsr, max_gsr, table_cap in _INJURY_CAP_TABLE:
                    if estimated_dnps >= min_dnps and min_gsr <= games_since_return <= max_gsr:
                        cap = table_cap
                        break
                return {
                    "is_returning":       True,
                    "consecutive_dnps":   estimated_dnps,
                    "approx_days_missed": round(gap_days, 1),
                    "games_since_return": games_since_return,
                    "cap_multiplier":     cap,
                    "reasoning":          f"injury_return_gap_{gap_days}d_g{games_since_return}_cap_{int(cap * 100)}pct",
                }
        return _no_cap

    # Last DNP date in the streak (between second_active and most_recent_active)
    try:
        last_dnp = max(
            d for d in dnp_dates
            if d < most_recent_active and (second_active is None or d > second_active)
        )
    except ValueError:
        return _no_cap

    # Count active games strictly after the last DNP (= games since return).
    # games_since_return = 1 means the most-recent game is the first game back.
    games_since_return = sum(1 for d in active_dates if d > last_dnp)

    approx_days_missed = round(consecutive_dnps * _AVG_DAYS_PER_DNP, 1)

    # Look up cap from table
    cap = 1.0
    for min_dnps, min_gsr, max_gsr, table_cap in _INJURY_CAP_TABLE:
        if consecutive_dnps >= min_dnps and min_gsr <= games_since_return <= max_gsr:
            cap = table_cap
            break

    return {
        "is_returning":       True,
        "consecutive_dnps":   consecutive_dnps,
        "approx_days_missed": approx_days_missed,
        "games_since_return": games_since_return,
        "cap_multiplier":     cap,
        "reasoning":          f"injury_return_g{games_since_return}:{int(cap * 100)}pct",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_minutes_multiplier(
    rolling: dict,
    logs: list,
    is_b2b: bool = False,
    splits: dict = None,
    excluded_games: list = None,
    roster_context: dict = None,
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
    roster_context : dict   optional usage adjustment output with massAbsenceTier

    Returns
    -------
    {
      "multiplier":        float,       # 0.50–1.15 (normal floor 0.85; injury cap floor 0.50)
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
    # Signal 1 — Volatility confidence adjustment
    # Confidence-only effect here; multiplier dampening happens after
    # signals 2-6 move the multiplier away from 1.0.
    # ------------------------------------------------------------------
    if cv > _VOLATILITY_HIGH:
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
            multiplier = max(_MULTIPLIER_NORMAL_MIN, multiplier * _STREAK_REDUCE)
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
    # Signal 5 — Starter-inferred role stability
    # Players averaging 28+ min with low CV are likely starters with
    # predictable minutes; boost their confidence.
    # ------------------------------------------------------------------
    if avg_s >= 28.0 and cv < _VOLATILITY_HIGH:
        confidence += 0.06
        reasoning.append("likely_starter")
    elif avg_s < 15.0:
        confidence -= 0.06
        reasoning.append("deep_bench")

    # ------------------------------------------------------------------
    # Signal 5b — Mass-absence minutes boost
    # When multiple starters are out, remaining players get expanded
    # minutes. Starters (avg_s >= 28) get a larger boost; promoted role
    # players (avg_s >= 20) get a smaller one since they absorb the
    # vacated minutes in extreme scenarios.
    # ------------------------------------------------------------------
    _mass_tier = (roster_context or {}).get("massAbsenceTier")
    if _mass_tier == "extreme":
        if avg_s >= 28.0:
            multiplier = min(_MULTIPLIER_MAX, multiplier * 1.06)
            confidence += 0.04
            reasoning.append(f"mass_absence_{_mass_tier}_starter")
        elif avg_s >= 20.0:
            multiplier = min(_MULTIPLIER_MAX, multiplier * 1.04)
            confidence += 0.02
            reasoning.append(f"mass_absence_{_mass_tier}_promoted")
    elif _mass_tier == "moderate" and avg_s >= 28.0:
        multiplier = min(_MULTIPLIER_MAX, multiplier * 1.03)
        reasoning.append(f"mass_absence_{_mass_tier}")

    # ------------------------------------------------------------------
    # Signal 6 — Sample size confidence
    # ------------------------------------------------------------------
    n = len(logs or [])
    if n < 5:
        confidence -= 0.15
        reasoning.append("small_sample")
    elif n >= 15:
        confidence += 0.05
        reasoning.append("large_sample")

    # ------------------------------------------------------------------
    # Volatility dampening (applied after signals 2-6 move multiplier)
    # High-CV players: compress multiplier 40% toward 1.0 to limit
    # extreme swings from streak/trend signals. Only fires when
    # multiplier has actually moved away from neutral.
    # ------------------------------------------------------------------
    if cv > _VOLATILITY_HIGH and abs(multiplier - 1.0) > 0.01:
        multiplier = 1.0 + (multiplier - 1.0) * 0.60
        reasoning.append("volatility_dampened")

    # ------------------------------------------------------------------
    # Signal 7 — Injury-return cap (hard ceiling; overrides streak/volatility)
    # ------------------------------------------------------------------
    if excluded_games is not None:
        injury = detect_injury_return(logs or [], excluded_games)
        if injury["is_returning"] and injury["cap_multiplier"] < 1.0:
            multiplier = min(multiplier, injury["cap_multiplier"])
            confidence = max(0.10, confidence - 0.12)
            reasoning.append(injury["reasoning"])

    # ------------------------------------------------------------------
    # Bounds
    # ------------------------------------------------------------------
    multiplier = max(_MULTIPLIER_ABSOLUTE_MIN, min(_MULTIPLIER_MAX, multiplier))
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
