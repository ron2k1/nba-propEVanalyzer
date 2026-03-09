#!/usr/bin/env python3
"""Risk metrics: drawdown, Sharpe, Calmar, streak analysis.

Pure computation module. No DB, no I/O, no side effects.
Takes a chronological list of bet records (same schema as
``_bet_record`` in ``nba_backtest.py``) and returns a full risk dict.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import mean, median, stdev


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_bets(
    bets: list[dict],
    policy_pass_only: bool = True,
    real_line_only: bool = False,
    stat: str | None = None,
) -> list[dict]:
    """Pre-filter bet records before computing risk metrics."""
    out = bets
    if policy_pass_only:
        out = [b for b in out if b.get("policy_pass")]
    if real_line_only:
        out = [b for b in out if b.get("used_real_line")]
    if stat:
        out = [b for b in out if b.get("stat") == stat]
    return out


# ---------------------------------------------------------------------------
# Deterministic sorting
# ---------------------------------------------------------------------------

def _sort_bets(bets: list[dict]) -> tuple[list[dict], str]:
    """Sort bets deterministically.

    Primary: date.  Secondary: bet_order (if present).
    Fallback: deterministic lexical key (player, stat, line, side).

    Returns ``(sorted_list, ordering_mode)``.
    """
    if not bets:
        return [], "empty"

    has_order = any("bet_order" in b for b in bets)
    if has_order:
        mode = "bet_order"
        key = lambda b: (b.get("date", ""), b.get("bet_order", 0))
    else:
        mode = "lexical_fallback"
        key = lambda b: (
            b.get("date", ""),
            str(b.get("player_name", b.get("player_id", ""))),
            b.get("stat", ""),
            str(b.get("line", "")),
            b.get("side", ""),
        )
    return sorted(bets, key=key), mode


# ---------------------------------------------------------------------------
# Daily PnL series
# ---------------------------------------------------------------------------

def _daily_pnl_series(
    bets: list[dict],
) -> list[tuple[str, float, int]]:
    """Group bets by date, sum PnL per day, fill gap days with zeros.

    Returns ``[(date_str, daily_pnl, num_bets), ...]`` sorted by date.
    """
    if not bets:
        return []

    by_day: dict[str, tuple[float, int]] = {}
    for b in bets:
        d = b["date"][:10]
        pnl_acc, cnt = by_day.get(d, (0.0, 0))
        by_day[d] = (pnl_acc + float(b.get("pnl", 0.0)), cnt + 1)

    dates = sorted(by_day)
    min_dt = datetime.strptime(dates[0], "%Y-%m-%d")
    max_dt = datetime.strptime(dates[-1], "%Y-%m-%d")

    result: list[tuple[str, float, int]] = []
    cur = min_dt
    while cur <= max_dt:
        ds = cur.strftime("%Y-%m-%d")
        if ds in by_day:
            result.append((ds, by_day[ds][0], by_day[ds][1]))
        else:
            result.append((ds, 0.0, 0))
        cur += timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------

def _max_drawdown(
    daily_series: list[tuple[str, float, int]],
    starting_bankroll: float,
) -> dict:
    """Compute max drawdown from daily PnL series.

    Denominator for pct: peak equity at the start of the drawdown period.
    """
    if not daily_series:
        return {
            "units": 0.0, "pct": 0.0, "peakEquityAtStart": starting_bankroll,
            "startDate": None, "troughDate": None,
            "recoveryDate": None, "recoveryDays": None,
        }

    cum_pnl = 0.0
    peak_equity = starting_bankroll
    peak_date = daily_series[0][0]

    max_dd_units = 0.0
    dd_start_date = None
    dd_trough_date = None
    dd_peak_equity_at_start = starting_bankroll

    # Track current drawdown
    cur_dd_start = None
    cur_peak_equity = starting_bankroll
    cur_peak_date = daily_series[0][0]

    # For recovery tracking
    recovered = True
    recovery_date = None

    for ds, pnl, _ in daily_series:
        cum_pnl += pnl
        equity = starting_bankroll + cum_pnl

        if equity >= peak_equity:
            # New high-water mark
            if not recovered and dd_start_date is not None:
                # We just recovered from the worst drawdown (if this IS the worst)
                recovery_date = ds
                recovered = True
            peak_equity = equity
            cur_peak_equity = equity
            cur_peak_date = ds
        else:
            dd = equity - peak_equity  # negative
            if recovered:
                # Start new drawdown period
                cur_dd_start = cur_peak_date
                recovered = False

            if dd < max_dd_units:
                max_dd_units = dd
                dd_start_date = cur_dd_start
                dd_trough_date = ds
                dd_peak_equity_at_start = peak_equity
                recovery_date = None  # reset — need to track recovery for THIS dd

    # Check if the max drawdown was recovered after the trough
    final_recovery = None
    if dd_start_date is not None and dd_trough_date is not None:
        # Re-scan from trough to find recovery
        cum2 = 0.0
        past_trough = False
        for ds, pnl, _ in daily_series:
            cum2 += pnl
            eq = starting_bankroll + cum2
            if ds == dd_trough_date:
                past_trough = True
                continue
            if past_trough and eq >= dd_peak_equity_at_start:
                final_recovery = ds
                break

    recovery_days = None
    if final_recovery and dd_trough_date:
        t0 = datetime.strptime(dd_trough_date, "%Y-%m-%d")
        t1 = datetime.strptime(final_recovery, "%Y-%m-%d")
        recovery_days = (t1 - t0).days

    dd_pct = 0.0
    if dd_peak_equity_at_start > 1e-9 and max_dd_units < 0:
        dd_pct = (max_dd_units / dd_peak_equity_at_start) * 100

    return {
        "units": round(max_dd_units, 2),
        "pct": round(dd_pct, 2),
        "peakEquityAtStart": round(dd_peak_equity_at_start, 2),
        "startDate": dd_start_date,
        "troughDate": dd_trough_date,
        "recoveryDate": final_recovery,
        "recoveryDays": recovery_days,
    }


# ---------------------------------------------------------------------------
# Sharpe ratio
# ---------------------------------------------------------------------------

def _sharpe_ratio(
    daily_series: list[tuple[str, float, int]],
    starting_bankroll: float,
    annualization_factor: float,
) -> dict:
    """Compute Sharpe ratio using return on at-risk capital.

    ``daily_return = daily_pnl / max(num_bets_that_day, 1)``
    This gives return per unit risked per day.
    """
    if len(daily_series) < 2:
        return {"daily": None, "annualized": None,
                "annualizationFactor": annualization_factor,
                "method": "return_on_risk", "reason": "insufficient_data"}

    returns = [pnl / max(n, 1) for _, pnl, n in daily_series]

    sd = stdev(returns)
    if sd < 1e-12:
        return {"daily": None, "annualized": None,
                "annualizationFactor": annualization_factor,
                "method": "return_on_risk", "reason": "zero_variance"}

    sharpe_daily = mean(returns) / sd
    sharpe_ann = sharpe_daily * math.sqrt(annualization_factor)

    # Secondary: bankroll-based Sharpe for comparison
    bankroll_sharpe = None
    if starting_bankroll > 1e-12:
        br_returns = [pnl / starting_bankroll for _, pnl, _ in daily_series]
        br_sd = stdev(br_returns)
        if br_sd > 1e-12:
            bankroll_sharpe = round(
                mean(br_returns) / br_sd * math.sqrt(annualization_factor), 4
            )

    return {
        "daily": round(sharpe_daily, 4),
        "annualized": round(sharpe_ann, 4),
        "annualizationFactor": annualization_factor,
        "method": "return_on_risk",
        "bankrollSharpe": bankroll_sharpe,
    }


# ---------------------------------------------------------------------------
# Calmar ratio
# ---------------------------------------------------------------------------

def _calmar_ratio(
    total_roi_pct: float,
    max_drawdown_pct: float,
    calendar_days: int = 0,
) -> dict:
    """Compute Calmar ratio and return-over-drawdown.

    ``calmar`` = annualized return / |max DD %| (standard definition).
    ``returnOverDrawdown`` = sample ROI / |max DD %| (non-annualized).
    Returns nulls with reason when DD is zero.
    """
    if abs(max_drawdown_pct) < 1e-9:
        return {
            "calmar": None,
            "returnOverDrawdown": None,
            "calendarDays": calendar_days,
            "method": "annualized_return_over_max_drawdown",
            "reason": "no_drawdown",
        }

    rod = total_roi_pct / abs(max_drawdown_pct)

    calmar_val = None
    if calendar_days > 0:
        annualized_roi = total_roi_pct * (365.0 / calendar_days)
        calmar_val = round(annualized_roi / abs(max_drawdown_pct), 4)

    return {
        "calmar": calmar_val,
        "returnOverDrawdown": round(rod, 4),
        "calendarDays": calendar_days,
        "method": "annualized_return_over_max_drawdown",
    }


# ---------------------------------------------------------------------------
# Streak analysis
# ---------------------------------------------------------------------------

def _streak_analysis(bets: list[dict]) -> dict:
    """Walk bets in order. Pushes are skipped (don't break/extend).

    Caller is expected to pass pre-sorted bets (by date + bet_order).
    """
    longest_win = 0
    longest_loss = 0
    lw_dates: list[str] = [None, None]
    ll_dates: list[str] = [None, None]

    cur_type: str | None = None  # "win" or "loss"
    cur_len = 0
    cur_start_date: str | None = None

    for b in bets:
        outcome = b.get("outcome", "").lower()
        if outcome == "push":
            continue
        d = b["date"][:10]
        if outcome == "win":
            if cur_type == "win":
                cur_len += 1
            else:
                cur_type = "win"
                cur_len = 1
                cur_start_date = d
            if cur_len > longest_win:
                longest_win = cur_len
                lw_dates = [cur_start_date, d]
        elif outcome == "loss":
            if cur_type == "loss":
                cur_len += 1
            else:
                cur_type = "loss"
                cur_len = 1
                cur_start_date = d
            if cur_len > longest_loss:
                longest_loss = cur_len
                ll_dates = [cur_start_date, d]

    return {
        "longestWin": longest_win,
        "longestWinDates": lw_dates if longest_win > 0 else None,
        "longestLoss": longest_loss,
        "longestLossDates": ll_dates if longest_loss > 0 else None,
        "currentStreak": (
            {"type": cur_type, "length": cur_len}
            if cur_type else None
        ),
    }


# ---------------------------------------------------------------------------
# Daily PnL summary
# ---------------------------------------------------------------------------

def _daily_pnl_summary(
    daily_series: list[tuple[str, float, int]],
) -> dict:
    """Aggregate daily PnL statistics."""
    if not daily_series:
        return {}

    pnls = [p for _, p, _ in daily_series]
    betting_days = [(d, p, n) for d, p, n in daily_series if n > 0]
    zero_bet_days = sum(1 for _, _, n in daily_series if n == 0)

    extrema_source = betting_days if betting_days else daily_series
    best = max(extrema_source, key=lambda x: x[1])
    worst = min(extrema_source, key=lambda x: x[1])

    winning_days = sum(1 for _, p, n in daily_series if p > 0 and n > 0)
    total_betting = len(betting_days)

    return {
        "mean": round(mean(pnls), 4) if pnls else 0.0,
        "median": round(median(pnls), 4) if pnls else 0.0,
        "stdev": round(stdev(pnls), 4) if len(pnls) >= 2 else 0.0,
        "bestDay": {"date": best[0], "pnl": round(best[1], 2), "bets": best[2]},
        "worstDay": {"date": worst[0], "pnl": round(worst[1], 2), "bets": worst[2]},
        "winningDaysPct": round(winning_days / total_betting * 100, 1) if total_betting else 0.0,
        "totalBettingDays": total_betting,
        "totalCalendarDays": len(daily_series),
        "zeroBetDays": zero_bet_days,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_risk_metrics(
    bets: list[dict],
    starting_bankroll: float = 100.0,
    annualization_factor: float = 180.0,
) -> dict:
    """Compute full risk metrics from a list of bet records.

    Parameters
    ----------
    bets : list[dict]
        Bet records with keys: date, pnl, outcome, policy_pass, used_real_line, stat.
        Need not be sorted — internal sorting is applied.
    starting_bankroll : float
        Notional starting bankroll in units (default 100).
    annualization_factor : float
        Trading days per year for Sharpe annualization (default 180 for NBA).

    Returns
    -------
    dict
        Full risk metrics dict.  ``{"riskMetrics": None, "reason": "no bets"}``
        when input is empty.
    """
    if not bets:
        return {"riskMetrics": None, "reason": "no bets"}

    # Deterministic sort before all computation
    sorted_bets, ordering_mode = _sort_bets(bets)

    total_pnl = sum(float(b.get("pnl", 0.0)) for b in sorted_bets)
    final_bankroll = starting_bankroll + total_pnl
    total_roi_pct = (total_pnl / starting_bankroll) * 100 if starting_bankroll > 0 else 0.0

    daily = _daily_pnl_series(sorted_bets)
    dd = _max_drawdown(daily, starting_bankroll)
    sharpe = _sharpe_ratio(daily, starting_bankroll, annualization_factor)
    calmar = _calmar_ratio(total_roi_pct, dd["pct"], len(daily))
    streaks = _streak_analysis(sorted_bets)
    daily_summary = _daily_pnl_summary(daily)

    return {
        "riskMetrics": {
            "equityCurve": {
                "startingBankroll": starting_bankroll,
                "finalBankroll": round(final_bankroll, 2),
                "totalPnlUnits": round(total_pnl, 2),
                "totalRoiPct": round(total_roi_pct, 2),
            },
            "maxDrawdown": dd,
            "sharpe": sharpe,
            "calmar": calmar.get("calmar"),
            "returnOverDrawdown": calmar.get("returnOverDrawdown"),
            "calmarDetail": calmar,
            "streaks": streaks,
            "dailyPnl": daily_summary,
            "betsAnalyzed": len(sorted_bets),
            "metadata": {
                "orderingMode": ordering_mode,
                "annualizationFactor": annualization_factor,
                "calmarMethod": "annualized_return_over_max_drawdown",
            },
        },
    }
