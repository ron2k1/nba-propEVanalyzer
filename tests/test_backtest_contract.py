"""
tests/test_backtest_contract.py — run_backtest() input validation and return
contract tests.

Exercises the public API of run_backtest() WITHOUT requiring network access,
SQLite databases, or real game data.  All tests use invalid dates or
validation-layer rejections that return before any I/O occurs.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.nba_backtest import run_backtest


class TestRunBacktestValidation(unittest.TestCase):
    """run_backtest() should reject bad inputs before doing any I/O."""

    def test_invalid_date_from(self):
        result = run_backtest("not-a-date")
        self.assertFalse(result["success"])
        self.assertIn("date_from", result["error"].lower())

    def test_invalid_date_to(self):
        result = run_backtest("2026-01-01", date_to="bad")
        self.assertFalse(result["success"])
        self.assertIn("date_to", result["error"].lower())

    def test_date_to_before_date_from(self):
        result = run_backtest("2026-02-01", date_to="2026-01-01")
        self.assertFalse(result["success"])
        self.assertIn("date_to", result["error"].lower())

    def test_invalid_model_rejected(self):
        result = run_backtest("2026-01-01", "2026-01-02", model="invalid")
        self.assertFalse(result["success"])
        self.assertIn("model", result["error"].lower())

    def test_invalid_data_source_rejected(self):
        result = run_backtest("2026-01-01", "2026-01-02", data_source="bad")
        self.assertFalse(result["success"])
        self.assertIn("data_source", result["error"].lower())

    def test_invalid_odds_source_rejected(self):
        result = run_backtest("2026-01-01", "2026-01-02", odds_source="bad")
        self.assertFalse(result["success"])
        self.assertIn("odds_source", result["error"].lower())

    def test_invalid_line_timing_rejected(self):
        result = run_backtest("2026-01-01", "2026-01-02", line_timing="bad")
        self.assertFalse(result["success"])
        self.assertIn("line_timing", result["error"].lower())

    def test_no_blend_requires_match_live(self):
        result = run_backtest("2026-01-01", "2026-01-02", no_blend=True)
        self.assertFalse(result["success"])
        self.assertIn("match-live", result["error"].lower())

    def test_no_gates_requires_match_live(self):
        result = run_backtest("2026-01-01", "2026-01-02", no_gates=True)
        self.assertFalse(result["success"])
        self.assertIn("match-live", result["error"].lower())

    def test_valid_model_values_accepted(self):
        """'full', 'simple', 'both' should all pass validation."""
        for model in ("full", "simple", "both"):
            result = run_backtest("2026-01-01", "2026-01-02", model=model,
                                  data_source="local")
            # May fail for other reasons (missing local index), but NOT
            # for invalid model.
            if not result["success"]:
                self.assertNotIn("model must be", result.get("error", ""))


class TestRunBacktestReturnContract(unittest.TestCase):
    """Verify the return dict always has 'success' and 'error' on failure."""

    REJECTION_CASES = [
        {"date_from": "bad"},
        {"date_from": "2026-01-01", "date_to": "bad"},
        {"date_from": "2026-02-01", "date_to": "2026-01-01"},
        {"date_from": "2026-01-01", "model": "bad"},
        {"date_from": "2026-01-01", "data_source": "bad"},
        {"date_from": "2026-01-01", "odds_source": "bad"},
        {"date_from": "2026-01-01", "line_timing": "bad"},
        {"date_from": "2026-01-01", "no_blend": True},
    ]

    def test_all_rejections_return_success_false_and_error(self):
        """Every validation rejection must return {success: False, error: str}."""
        for i, kwargs in enumerate(self.REJECTION_CASES):
            with self.subTest(case=i, kwargs=kwargs):
                result = run_backtest(**kwargs)
                self.assertIsInstance(result, dict, f"Case {i}: must return dict")
                self.assertFalse(result.get("success"), f"Case {i}: must be False")
                self.assertIn("error", result, f"Case {i}: must have 'error' key")
                self.assertIsInstance(result["error"], str, f"Case {i}: error must be str")


if __name__ == "__main__":
    unittest.main()
