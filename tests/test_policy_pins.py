"""
tests/test_policy_pins.py — Wave 5 policy constant pins.

Pins every exported constant in core/policy_config.py to its current value.
Any unauthorized change to a policy constant will cause these tests to fail,
providing an early warning before the change reaches production.

These are NOT freeze-period tests — they are permanent guardrails.  If a
policy constant needs to change, the corresponding test must be updated
in the same commit with a comment explaining the rationale.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.policy_config import (
    STAT_WHITELIST,
    BLOCKED_PROB_BINS,
    MIN_EV_PCT,
    ELIGIBLE_STATS,
    MIN_EDGE,
    MIN_EDGE_BY_STAT,
    MIN_CONFIDENCE,
    REAL_LINE_REQUIRED_STATS,
    MIN_GAMES_PLAYED,
    MIN_SEASON_AVG_MINUTES,
    PINNACLE_THRESHOLDS,
    PINNACLE_MIN_NO_VIG_BY_STAT,
)


class TestPolicyPins(unittest.TestCase):
    """Pin all policy constants to their documented values."""

    # -------------------------------------------------------------------
    # BETTING_POLICY pins
    # -------------------------------------------------------------------

    def test_stat_whitelist_pinned(self):
        """Only pts and ast are whitelisted for real-money bets."""
        self.assertEqual(STAT_WHITELIST, {"pts", "ast"})

    def test_blocked_bins_pinned(self):
        """Bins 1-8 are blocked; only bins 0 (UNDER) and 9 (OVER) are active."""
        self.assertEqual(BLOCKED_PROB_BINS, {1, 2, 3, 4, 5, 6, 7, 8})

    def test_min_ev_pct_pinned(self):
        """Minimum EV percentage threshold is 0.0."""
        self.assertEqual(MIN_EV_PCT, 0.0)

    # -------------------------------------------------------------------
    # SIGNAL_SPEC pins
    # -------------------------------------------------------------------

    def test_eligible_stats_pinned(self):
        """Eligible stats for signal logging: pts, reb, ast."""
        self.assertEqual(ELIGIBLE_STATS, {"pts", "reb", "ast"})

    def test_min_edge_pinned(self):
        """Global min edge threshold raised to 0.08 on 2026-03-01."""
        self.assertEqual(MIN_EDGE, 0.08)

    def test_min_edge_by_stat_pinned(self):
        """Per-stat edge overrides: reb=0.08, ast=0.09."""
        self.assertEqual(MIN_EDGE_BY_STAT, {"reb": 0.08, "ast": 0.09})

    def test_min_confidence_pinned(self):
        """Minimum confidence raised to 0.60 on 2026-03-01."""
        self.assertEqual(MIN_CONFIDENCE, 0.60)

    def test_real_line_required_stats_pinned(self):
        """Only reb requires a real line (not synthetic) to qualify."""
        self.assertEqual(REAL_LINE_REQUIRED_STATS, {"reb"})

    # -------------------------------------------------------------------
    # Player quality gate pins
    # -------------------------------------------------------------------

    def test_min_games_played_pinned(self):
        """Minimum 10 games played to filter deep-bench / callup players."""
        self.assertEqual(MIN_GAMES_PLAYED, 10)

    def test_min_season_avg_minutes_pinned(self):
        """Minimum 10.0 season average minutes per game."""
        self.assertEqual(MIN_SEASON_AVG_MINUTES, 10.0)

    # -------------------------------------------------------------------
    # Pinnacle confirmation pins
    # -------------------------------------------------------------------

    def test_pinnacle_thresholds_pinned(self):
        """Pinnacle thresholds for bins 0 and 9."""
        self.assertEqual(PINNACLE_THRESHOLDS, {0: 0.75, 9: 0.75})

    def test_pinnacle_min_no_vig_by_stat_pinned(self):
        """Per-stat Pinnacle minimum no-vig probability thresholds."""
        self.assertEqual(
            PINNACLE_MIN_NO_VIG_BY_STAT,
            {"pts": 0.62, "ast": 0.67, "reb": 0.62},
        )

    # -------------------------------------------------------------------
    # Structural invariants
    # -------------------------------------------------------------------

    def test_stat_whitelist_is_subset_of_eligible(self):
        """Whitelist must be a subset of eligible stats (can't bet on unlogged stat)."""
        self.assertTrue(
            STAT_WHITELIST.issubset(ELIGIBLE_STATS),
            f"STAT_WHITELIST {STAT_WHITELIST} is not a subset of "
            f"ELIGIBLE_STATS {ELIGIBLE_STATS}",
        )

    def test_real_line_required_is_subset_of_eligible(self):
        """Real-line-required stats must be eligible."""
        self.assertTrue(
            REAL_LINE_REQUIRED_STATS.issubset(ELIGIBLE_STATS),
            f"REAL_LINE_REQUIRED_STATS {REAL_LINE_REQUIRED_STATS} is not a subset "
            f"of ELIGIBLE_STATS {ELIGIBLE_STATS}",
        )

    def test_blocked_bins_are_contiguous_middle(self):
        """Blocked bins should be the contiguous range 1-8, leaving 0 and 9 open."""
        self.assertEqual(BLOCKED_PROB_BINS, set(range(1, 9)))

    def test_min_edge_by_stat_values_at_least_global(self):
        """Per-stat edge overrides must be >= global MIN_EDGE."""
        for stat, edge in MIN_EDGE_BY_STAT.items():
            self.assertGreaterEqual(
                edge, MIN_EDGE,
                f"MIN_EDGE_BY_STAT['{stat}']={edge} is below global MIN_EDGE={MIN_EDGE}",
            )


if __name__ == "__main__":
    unittest.main()
