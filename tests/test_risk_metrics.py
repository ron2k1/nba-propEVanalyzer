#!/usr/bin/env python3
"""Tests for core.nba_risk_metrics — drawdown, Sharpe, Calmar, streaks."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.nba_risk_metrics import (
    _calmar_ratio,
    _daily_pnl_series,
    _daily_pnl_summary,
    _max_drawdown,
    _sharpe_ratio,
    _sort_bets,
    _streak_analysis,
    compute_risk_metrics,
    filter_bets,
)
from nba_cli.risk_commands import _extract_bets, _handle_risk_metrics


def _bet(date, pnl, outcome="win", stat="pts", policy_pass=True,
         used_real_line=True, bet_order=None, player_name=None):
    """Convenience factory for test bet records."""
    rec = {
        "date": date,
        "pnl": pnl,
        "outcome": outcome,
        "stat": stat,
        "policy_pass": policy_pass,
        "used_real_line": used_real_line,
    }
    if bet_order is not None:
        rec["bet_order"] = bet_order
    if player_name is not None:
        rec["player_name"] = player_name
    return rec


class TestFilterBets(unittest.TestCase):
    def test_policy_filter(self):
        bets = [_bet("2026-01-01", 1.0, policy_pass=True),
                _bet("2026-01-01", -1.0, policy_pass=False)]
        self.assertEqual(len(filter_bets(bets, policy_pass_only=True)), 1)
        self.assertEqual(len(filter_bets(bets, policy_pass_only=False)), 2)

    def test_real_line_filter(self):
        bets = [_bet("2026-01-01", 1.0, used_real_line=True),
                _bet("2026-01-01", -1.0, used_real_line=False)]
        self.assertEqual(len(filter_bets(bets, policy_pass_only=False,
                                         real_line_only=True)), 1)

    def test_stat_filter(self):
        bets = [_bet("2026-01-01", 1.0, stat="pts"),
                _bet("2026-01-01", -1.0, stat="ast")]
        self.assertEqual(len(filter_bets(bets, policy_pass_only=False,
                                         stat="pts")), 1)


class TestSortBets(unittest.TestCase):
    def test_bet_order_mode(self):
        bets = [
            _bet("2026-01-01", 1.0, bet_order=2),
            _bet("2026-01-01", -1.0, bet_order=0),
            _bet("2026-01-01", 0.5, bet_order=1),
        ]
        sorted_bets, mode = _sort_bets(bets)
        self.assertEqual(mode, "bet_order")
        self.assertEqual([b["bet_order"] for b in sorted_bets], [0, 1, 2])

    def test_lexical_fallback_mode(self):
        bets = [
            _bet("2026-01-01", 1.0, player_name="Zion"),
            _bet("2026-01-01", -1.0, player_name="Ant"),
        ]
        sorted_bets, mode = _sort_bets(bets)
        self.assertEqual(mode, "lexical_fallback")
        self.assertEqual(sorted_bets[0]["player_name"], "Ant")
        self.assertEqual(sorted_bets[1]["player_name"], "Zion")

    def test_empty(self):
        sorted_bets, mode = _sort_bets([])
        self.assertEqual(sorted_bets, [])
        self.assertEqual(mode, "empty")

    def test_date_is_primary_key(self):
        bets = [
            _bet("2026-01-02", 1.0, bet_order=0),
            _bet("2026-01-01", -1.0, bet_order=1),
        ]
        sorted_bets, _ = _sort_bets(bets)
        self.assertEqual(sorted_bets[0]["date"], "2026-01-01")
        self.assertEqual(sorted_bets[1]["date"], "2026-01-02")


class TestDailyPnlSeries(unittest.TestCase):
    def test_basic_with_gap(self):
        bets = [
            _bet("2026-01-01", 1.0),
            _bet("2026-01-01", 0.5),
            _bet("2026-01-02", -1.0),
            # gap: 2026-01-03
            _bet("2026-01-04", 2.0),
            _bet("2026-01-04", -0.5),
        ]
        series = _daily_pnl_series(bets)
        self.assertEqual(len(series), 4)  # 4 days incl gap
        self.assertAlmostEqual(series[0][1], 1.5)   # Jan 1
        self.assertEqual(series[0][2], 2)            # 2 bets
        self.assertAlmostEqual(series[1][1], -1.0)   # Jan 2
        self.assertAlmostEqual(series[2][1], 0.0)    # Jan 3 gap
        self.assertEqual(series[2][2], 0)             # 0 bets
        self.assertAlmostEqual(series[3][1], 1.5)    # Jan 4

    def test_empty(self):
        self.assertEqual(_daily_pnl_series([]), [])


class TestMaxDrawdown(unittest.TestCase):
    def test_known_curve(self):
        # Equity: 100 -> 102 -> 105 -> 104 -> 100 -> 105
        # Drawdown from 105: -5 units = -4.76%
        series = [
            ("2026-01-01", 2.0, 1),
            ("2026-01-02", 3.0, 1),
            ("2026-01-03", -1.0, 1),
            ("2026-01-04", -4.0, 1),
            ("2026-01-05", 5.0, 1),
        ]
        dd = _max_drawdown(series, 100.0)
        self.assertAlmostEqual(dd["units"], -5.0)
        self.assertAlmostEqual(dd["pct"], -5.0 / 105.0 * 100, places=2)
        self.assertEqual(dd["startDate"], "2026-01-02")  # peak equity at 01-02
        self.assertEqual(dd["troughDate"], "2026-01-04")
        self.assertEqual(dd["recoveryDate"], "2026-01-05")
        self.assertIsNotNone(dd["recoveryDays"])

    def test_no_drawdown(self):
        series = [
            ("2026-01-01", 1.0, 1),
            ("2026-01-02", 2.0, 1),
            ("2026-01-03", 3.0, 1),
        ]
        dd = _max_drawdown(series, 100.0)
        self.assertAlmostEqual(dd["units"], 0.0)
        self.assertAlmostEqual(dd["pct"], 0.0)

    def test_unrecovered(self):
        series = [
            ("2026-01-01", 5.0, 1),
            ("2026-01-02", -10.0, 1),
            ("2026-01-03", 2.0, 1),
        ]
        dd = _max_drawdown(series, 100.0)
        self.assertTrue(dd["units"] < 0)
        self.assertIsNone(dd["recoveryDate"])
        self.assertIsNone(dd["recoveryDays"])

    def test_early_losses(self):
        # Start losing immediately — peak equity == starting bankroll
        series = [
            ("2026-01-01", -5.0, 1),
            ("2026-01-02", -3.0, 1),
        ]
        dd = _max_drawdown(series, 100.0)
        self.assertAlmostEqual(dd["units"], -8.0)
        self.assertAlmostEqual(dd["peakEquityAtStart"], 100.0)

    def test_empty(self):
        dd = _max_drawdown([], 100.0)
        self.assertEqual(dd["units"], 0.0)


class TestSharpeRatio(unittest.TestCase):
    def test_positive_sharpe(self):
        # All positive PnL -> positive Sharpe
        series = [
            ("2026-01-01", 1.0, 1),
            ("2026-01-02", 2.0, 2),
            ("2026-01-03", 0.5, 1),
            ("2026-01-04", 1.5, 1),
        ]
        s = _sharpe_ratio(series, 100.0, 180.0)
        self.assertIsNotNone(s["daily"])
        self.assertGreater(s["daily"], 0)
        self.assertIsNotNone(s["annualized"])
        self.assertEqual(s["method"], "return_on_risk")

    def test_insufficient_data(self):
        series = [("2026-01-01", 1.0, 1)]
        s = _sharpe_ratio(series, 100.0, 180.0)
        self.assertIsNone(s["daily"])
        self.assertEqual(s["reason"], "insufficient_data")

    def test_zero_variance(self):
        series = [
            ("2026-01-01", 1.0, 1),
            ("2026-01-02", 1.0, 1),
            ("2026-01-03", 1.0, 1),
        ]
        s = _sharpe_ratio(series, 100.0, 180.0)
        self.assertIsNone(s["daily"])
        self.assertEqual(s["reason"], "zero_variance")

    def test_multi_bet_day_normalization(self):
        # 2 bets on day 1 with PnL 4 -> return_on_risk = 4/2 = 2
        series = [
            ("2026-01-01", 4.0, 2),
            ("2026-01-02", 1.0, 1),
        ]
        s = _sharpe_ratio(series, 100.0, 180.0)
        self.assertIsNotNone(s["daily"])


class TestCalmarRatio(unittest.TestCase):
    def test_normal(self):
        c = _calmar_ratio(40.0, -8.0, 100)
        # returnOverDrawdown = 40.0 / 8.0 = 5.0
        self.assertAlmostEqual(c["returnOverDrawdown"], 5.0)
        # calmar = (40.0 * 365/100) / 8.0 = 146.0 / 8.0 = 18.25
        self.assertAlmostEqual(c["calmar"], 18.25)
        self.assertEqual(c["method"], "annualized_return_over_max_drawdown")

    def test_zero_drawdown(self):
        c = _calmar_ratio(40.0, 0.0, 100)
        self.assertIsNone(c["calmar"])
        self.assertIsNone(c["returnOverDrawdown"])
        self.assertEqual(c["reason"], "no_drawdown")

    def test_no_calendar_days(self):
        c = _calmar_ratio(40.0, -8.0, 0)
        self.assertIsNone(c["calmar"])  # can't annualize without days
        self.assertAlmostEqual(c["returnOverDrawdown"], 5.0)


class TestStreakAnalysis(unittest.TestCase):
    def test_mixed_with_push(self):
        bets = [
            _bet("2026-01-01", 1.0, "win"),
            _bet("2026-01-01", 1.0, "win"),
            _bet("2026-01-02", -1.0, "loss"),
            _bet("2026-01-02", 0.0, "push"),   # push skipped
            _bet("2026-01-03", 1.0, "win"),
            _bet("2026-01-03", 1.0, "win"),
            _bet("2026-01-03", 1.0, "win"),
            _bet("2026-01-04", -1.0, "loss"),
            _bet("2026-01-04", -1.0, "loss"),
        ]
        s = _streak_analysis(bets)
        self.assertEqual(s["longestWin"], 3)
        self.assertEqual(s["longestLoss"], 2)
        self.assertEqual(s["currentStreak"]["type"], "loss")
        self.assertEqual(s["currentStreak"]["length"], 2)

    def test_all_wins(self):
        bets = [_bet(f"2026-01-0{i}", 1.0, "win") for i in range(1, 6)]
        s = _streak_analysis(bets)
        self.assertEqual(s["longestWin"], 5)
        self.assertEqual(s["longestLoss"], 0)

    def test_all_losses(self):
        bets = [_bet(f"2026-01-0{i}", -1.0, "loss") for i in range(1, 4)]
        s = _streak_analysis(bets)
        self.assertEqual(s["longestLoss"], 3)
        self.assertEqual(s["longestWin"], 0)

    def test_empty(self):
        s = _streak_analysis([])
        self.assertEqual(s["longestWin"], 0)
        self.assertIsNone(s["currentStreak"])

    def test_bet_order_controls_streak(self):
        """bet_order determines intra-day sequence — affects streak detection."""
        # Without bet_order, lexical fallback might reorder these differently
        bets = [
            _bet("2026-01-01", 1.0, "win", bet_order=0),
            _bet("2026-01-01", 1.0, "win", bet_order=1),
            _bet("2026-01-01", -1.0, "loss", bet_order=2),
        ]
        sorted_bets, mode = _sort_bets(bets)
        s = _streak_analysis(sorted_bets)
        self.assertEqual(mode, "bet_order")
        self.assertEqual(s["longestWin"], 2)
        self.assertEqual(s["longestLoss"], 1)
        self.assertEqual(s["currentStreak"]["type"], "loss")


class TestComputeRiskMetrics(unittest.TestCase):
    def test_empty_bets(self):
        result = compute_risk_metrics([])
        self.assertIsNone(result["riskMetrics"])
        self.assertEqual(result["reason"], "no bets")

    def test_single_win(self):
        bets = [_bet("2026-01-01", 0.91, "win")]
        result = compute_risk_metrics(bets, starting_bankroll=100.0)
        rm = result["riskMetrics"]
        self.assertIsNotNone(rm)
        self.assertAlmostEqual(rm["equityCurve"]["totalPnlUnits"], 0.91)
        self.assertEqual(rm["betsAnalyzed"], 1)
        self.assertEqual(rm["streaks"]["longestWin"], 1)

    def test_single_loss(self):
        bets = [_bet("2026-01-01", -1.0, "loss")]
        result = compute_risk_metrics(bets, starting_bankroll=100.0)
        rm = result["riskMetrics"]
        self.assertAlmostEqual(rm["maxDrawdown"]["units"], -1.0)
        self.assertEqual(rm["streaks"]["longestLoss"], 1)

    def test_all_wins_no_drawdown(self):
        bets = [_bet(f"2026-01-0{i}", 1.0, "win") for i in range(1, 6)]
        result = compute_risk_metrics(bets)
        rm = result["riskMetrics"]
        self.assertAlmostEqual(rm["maxDrawdown"]["units"], 0.0)
        self.assertIsNone(rm["calmar"])
        self.assertIsNone(rm["returnOverDrawdown"])
        self.assertEqual(rm["calmarDetail"]["reason"], "no_drawdown")

    def test_full_output_keys(self):
        bets = [
            _bet("2026-01-01", 1.0, "win"),
            _bet("2026-01-02", -0.5, "loss"),
            _bet("2026-01-03", 2.0, "win"),
            _bet("2026-01-04", 0.8, "win"),
        ]
        result = compute_risk_metrics(bets)
        rm = result["riskMetrics"]
        expected_keys = {
            "equityCurve", "maxDrawdown", "sharpe", "calmar",
            "returnOverDrawdown", "calmarDetail", "streaks",
            "dailyPnl", "betsAnalyzed", "metadata",
        }
        self.assertEqual(set(rm.keys()), expected_keys)

    def test_roi_calculation(self):
        bets = [_bet("2026-01-01", 50.0, "win")]
        result = compute_risk_metrics(bets, starting_bankroll=100.0)
        rm = result["riskMetrics"]
        self.assertAlmostEqual(rm["equityCurve"]["totalRoiPct"], 50.0)
        self.assertAlmostEqual(rm["equityCurve"]["finalBankroll"], 150.0)

    def test_metadata_present(self):
        bets = [_bet("2026-01-01", 1.0, "win", bet_order=0)]
        result = compute_risk_metrics(bets)
        rm = result["riskMetrics"]
        self.assertIn("metadata", rm)
        self.assertEqual(rm["metadata"]["orderingMode"], "bet_order")
        self.assertEqual(rm["metadata"]["annualizationFactor"], 180.0)
        self.assertEqual(rm["metadata"]["calmarMethod"],
                         "annualized_return_over_max_drawdown")

    def test_lexical_fallback_mode_in_metadata(self):
        bets = [_bet("2026-01-01", 1.0, "win")]  # no bet_order
        result = compute_risk_metrics(bets)
        rm = result["riskMetrics"]
        self.assertEqual(rm["metadata"]["orderingMode"], "lexical_fallback")

    def test_unsorted_bets_produce_correct_result(self):
        """Bets given out of date order are internally sorted."""
        ordered = [
            _bet("2026-01-01", 1.0, "win"),
            _bet("2026-01-02", -0.5, "loss"),
            _bet("2026-01-03", 2.0, "win"),
        ]
        reversed_input = list(reversed(ordered))
        r1 = compute_risk_metrics(ordered)
        r2 = compute_risk_metrics(reversed_input)
        self.assertEqual(r1["riskMetrics"]["equityCurve"],
                         r2["riskMetrics"]["equityCurve"])
        self.assertEqual(r1["riskMetrics"]["maxDrawdown"],
                         r2["riskMetrics"]["maxDrawdown"])
        self.assertEqual(r1["riskMetrics"]["streaks"],
                         r2["riskMetrics"]["streaks"])

    def test_determinism_with_bet_order(self):
        """Same bets in different insertion order produce identical metrics."""
        bets_a = [
            _bet("2026-01-01", 1.0, "win", bet_order=0),
            _bet("2026-01-01", -1.0, "loss", bet_order=1),
            _bet("2026-01-02", 0.5, "win", bet_order=2),
        ]
        bets_b = [bets_a[1], bets_a[2], bets_a[0]]  # scrambled
        r1 = compute_risk_metrics(bets_a)
        r2 = compute_risk_metrics(bets_b)
        self.assertEqual(r1["riskMetrics"]["streaks"],
                         r2["riskMetrics"]["streaks"])
        self.assertEqual(r1["riskMetrics"]["equityCurve"],
                         r2["riskMetrics"]["equityCurve"])

    def test_zero_bankroll_is_safe(self):
        bets = [
            _bet("2026-01-01", 1.0, "win"),
            _bet("2026-01-02", -0.5, "loss"),
        ]
        result = compute_risk_metrics(bets, starting_bankroll=0.0)
        rm = result["riskMetrics"]
        self.assertEqual(rm["equityCurve"]["startingBankroll"], 0.0)
        self.assertEqual(rm["equityCurve"]["totalRoiPct"], 0.0)
        self.assertIsNone(rm["sharpe"]["bankrollSharpe"])


class TestDailyPnlSummary(unittest.TestCase):
    def test_summary_stats(self):
        series = [
            ("2026-01-01", 2.0, 2),
            ("2026-01-02", 0.0, 0),   # gap day
            ("2026-01-03", -1.0, 1),
            ("2026-01-04", 3.0, 3),
        ]
        s = _daily_pnl_summary(series)
        self.assertEqual(s["bestDay"]["date"], "2026-01-04")
        self.assertEqual(s["worstDay"]["date"], "2026-01-03")
        self.assertEqual(s["totalBettingDays"], 3)
        self.assertEqual(s["zeroBetDays"], 1)
        self.assertEqual(s["totalCalendarDays"], 4)

    def test_best_and_worst_ignore_gap_days(self):
        series = [
            ("2026-01-01", 1.0, 1),
            ("2026-01-02", 0.0, 0),   # gap day
            ("2026-01-03", 2.0, 1),
        ]
        s = _daily_pnl_summary(series)
        self.assertEqual(s["bestDay"]["date"], "2026-01-03")
        self.assertEqual(s["worstDay"]["date"], "2026-01-01")


# ---------------------------------------------------------------------------
# Phase 3: CLI artifact extraction tests
# ---------------------------------------------------------------------------

class TestExtractBets(unittest.TestCase):
    def test_single_model_list(self):
        data = {"bets": [{"date": "2026-01-01", "pnl": 1.0}]}
        bets, err = _extract_bets(data)
        self.assertIsNone(err)
        self.assertEqual(len(bets), 1)

    def test_multi_model_dict_with_flag(self):
        data = {"bets": {
            "full": [{"date": "2026-01-01", "pnl": 1.0}],
            "simple": [{"date": "2026-01-01", "pnl": 0.5}],
        }}
        bets, err = _extract_bets(data, model="full")
        self.assertIsNone(err)
        self.assertEqual(bets[0]["pnl"], 1.0)

    def test_multi_model_dict_without_flag_errors(self):
        data = {"bets": {
            "full": [{"date": "2026-01-01", "pnl": 1.0}],
            "simple": [{"date": "2026-01-01", "pnl": 0.5}],
        }}
        bets, err = _extract_bets(data)
        self.assertIsNone(bets)
        self.assertIn("--model", err["error"])

    def test_multi_model_single_key_auto_selects(self):
        data = {"bets": {
            "full": [{"date": "2026-01-01", "pnl": 1.0}],
        }}
        bets, err = _extract_bets(data)
        self.assertIsNone(err)
        self.assertEqual(len(bets), 1)

    def test_missing_model_in_dict(self):
        data = {"bets": {
            "full": [{"date": "2026-01-01", "pnl": 1.0}],
        }}
        bets, err = _extract_bets(data, model="simple")
        self.assertIsNone(bets)
        self.assertIn("simple", err["error"])

    def test_fallback_to_reports(self):
        data = {
            "reports": {
                "full": {"bets": [{"date": "2026-01-01", "pnl": 1.0}]},
            }
        }
        bets, err = _extract_bets(data)
        self.assertIsNone(err)
        self.assertEqual(len(bets), 1)

    def test_fallback_to_reports_with_model(self):
        data = {
            "reports": {
                "full": {"bets": [{"date": "2026-01-01", "pnl": 1.0}]},
                "simple": {"bets": [{"date": "2026-01-01", "pnl": 0.5}]},
            }
        }
        bets, err = _extract_bets(data, model="simple")
        self.assertIsNone(err)
        self.assertEqual(bets[0]["pnl"], 0.5)

    def test_no_bets_anywhere(self):
        data = {"reports": {"full": {"hitRate": 0.5}}}
        bets, err = _extract_bets(data)
        self.assertIsNone(bets)
        self.assertIn("No 'bets'", err["error"])

    def test_empty_data(self):
        data = {}
        bets, err = _extract_bets(data)
        self.assertIsNone(bets)
        self.assertIn("No 'bets'", err["error"])


class TestRiskMetricsCommand(unittest.TestCase):
    def _write_artifact(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_default_is_policy_only(self):
        path = self._write_artifact({
            "bets": [
                _bet("2026-01-01", 1.0, "win", policy_pass=True),
                _bet("2026-01-02", -1.0, "loss", policy_pass=False),
            ]
        })
        result = _handle_risk_metrics(["nba_mod.py", "risk_metrics", path])
        self.assertEqual(result["filters"]["policyPassOnly"], True)
        self.assertEqual(result["riskMetrics"]["betsAnalyzed"], 1)

    def test_policy_only_flag_filters_bets(self):
        path = self._write_artifact({
            "bets": [
                _bet("2026-01-01", 1.0, "win", policy_pass=True),
                _bet("2026-01-02", -1.0, "loss", policy_pass=False),
            ]
        })
        result = _handle_risk_metrics(
            ["nba_mod.py", "risk_metrics", path, "--policy-only"]
        )
        self.assertEqual(result["filters"]["policyPassOnly"], True)
        self.assertEqual(result["riskMetrics"]["betsAnalyzed"], 1)

    def test_all_bets_override_includes_non_policy_bets(self):
        path = self._write_artifact({
            "bets": [
                _bet("2026-01-01", 1.0, "win", policy_pass=True),
                _bet("2026-01-02", -1.0, "loss", policy_pass=False),
            ]
        })
        result = _handle_risk_metrics(
            ["nba_mod.py", "risk_metrics", path, "--all-bets"]
        )
        self.assertEqual(result["filters"]["policyPassOnly"], False)
        self.assertEqual(result["riskMetrics"]["betsAnalyzed"], 2)

    def test_conflicting_policy_flags_are_rejected(self):
        path = self._write_artifact({
            "bets": [_bet("2026-01-01", 1.0, "win")]
        })
        result = _handle_risk_metrics(
            ["nba_mod.py", "risk_metrics", path, "--policy-only", "--all-bets"]
        )
        self.assertIn("either --policy-only or --all-bets", result["error"])

    def test_non_positive_bankroll_is_rejected(self):
        path = self._write_artifact({
            "bets": [_bet("2026-01-01", 1.0, "win")]
        })
        result = _handle_risk_metrics(
            ["nba_mod.py", "risk_metrics", path, "--bankroll", "0"]
        )
        self.assertIn("bankroll", result["error"].lower())


class TestIntegrationWithBacktest(unittest.TestCase):
    """Load a real backtest file if available."""

    def _find_backtest(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        results_dir = os.path.join(root, "data", "backtest_results")
        if not os.path.isdir(results_dir):
            return None
        for f in sorted(os.listdir(results_dir), reverse=True):
            if f.endswith(".json") and not f.startswith("ckpt_"):
                path = os.path.join(results_dir, f)
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                # Check for bets in top level or in reports
                if data.get("bets"):
                    return data
                for rpt in data.get("reports", {}).values():
                    if rpt.get("bets"):
                        return data
        return None

    def test_real_backtest_if_available(self):
        data = self._find_backtest()
        if data is None:
            self.skipTest("No backtest with bets found in data/backtest_results/")

        bets, err = _extract_bets(data)
        if err:
            # Try with model flag for multi-model artifacts
            bets, err = _extract_bets(data, model="full")
        if err:
            self.skipTest(f"Could not extract bets: {err}")

        result = compute_risk_metrics(bets)
        rm = result["riskMetrics"]
        self.assertIsNotNone(rm)
        self.assertIn("equityCurve", rm)
        self.assertIn("maxDrawdown", rm)
        self.assertIn("sharpe", rm)
        self.assertIn("streaks", rm)
        self.assertIn("dailyPnl", rm)
        self.assertIn("metadata", rm)
        self.assertIn("returnOverDrawdown", rm)
        self.assertGreater(rm["betsAnalyzed"], 0)

    def test_extract_bets_on_real_artifact(self):
        """Verify _extract_bets handles the real artifact shape without crash."""
        data = self._find_backtest()
        if data is None:
            self.skipTest("No backtest with bets found in data/backtest_results/")

        # First try without model
        bets, err = _extract_bets(data)
        if err and "--model" in err.get("error", ""):
            # Multi-model: try with model flag
            bets, err = _extract_bets(data, model="full")
        self.assertIsNone(err, f"_extract_bets failed: {err}")
        self.assertIsInstance(bets, list)
        self.assertGreater(len(bets), 0)


if __name__ == "__main__":
    unittest.main()
