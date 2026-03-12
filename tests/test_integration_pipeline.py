#!/usr/bin/env python3
"""
Integration tests for the Cal -> EV -> Gates and Odds -> CLV -> Gates pipelines.

These tests verify that multi-layer pipelines compose correctly:
- Calibration -> EV Engine -> Gates: temperature scaling, probability math, gate filtering
- OddsStore -> CLV -> Gates: closing lines, line value, gate logic
"""

import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.nba_ev_engine import compute_ev, _PROB_CAL
from core.gates import _qualifies, SIGNAL_SPEC, CURRENT_SIGNAL_VERSION
from core.policy_config import BLOCKED_PROB_BINS, STAT_WHITELIST

_CAL_PATH = os.path.join(ROOT, "models", "prob_calibration.json")

_ODDS_DB_PATH = os.path.join(
    ROOT, "data", "reference", "odds_history", "odds_history.sqlite"
)


def _has_calibration():
    """Return True if prod calibration file exists and loaded successfully."""
    return os.path.isfile(_CAL_PATH) and bool(_PROB_CAL)


def _wrap_ev_for_gate(ev_result: dict) -> dict:
    """
    Wrap a raw compute_ev() result dict into the prop_result shape that
    _qualifies() expects: prop_result["ev"] = <compute_ev output>.

    This mirrors how the live pipeline embeds EV output into the broader
    prop_result dict before passing it through gates.
    """
    return {"ev": ev_result}


# ===========================================================================
# Cal -> EV -> Gates pipeline
# ===========================================================================

class TestCalEVGatesPipeline(unittest.TestCase):
    """
    Integration tests for the Calibration -> EV Engine -> Gates pipeline.

    Each test exercises at least two of the three layers together, verifying
    that data flows correctly across boundaries.
    """

    # ------------------------------------------------------------------
    # 1. Calibration changes the EV output
    # ------------------------------------------------------------------
    def test_calibrated_ev_changes_verdict(self):
        """
        Load real prod calibration, compute_ev for pts with and without
        stat= param.  The calibrated version (stat='pts') must produce a
        different probOver than the uncalibrated version (stat=None).

        This confirms that temperature scaling is actually applied when
        stat= is provided and that the calibration file is loaded.
        """
        if not _has_calibration():
            self.skipTest("models/prob_calibration.json missing or empty")

        # A projection clearly above the line so raw probOver is well above 0.5.
        # Temperature > 1 shrinks probabilities toward 0.5, so calibrated
        # probOver should be strictly lower than raw probOver.
        common = dict(
            projection=30.0,
            line=24.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.0,
        )

        result_raw = compute_ev(**common, stat=None)
        result_cal = compute_ev(**common, stat="pts")

        self.assertIsNotNone(result_raw)
        self.assertIsNotNone(result_cal)

        prob_raw = result_raw["probOver"]
        prob_cal = result_cal["probOver"]

        # With pts temperature = 1.81 (>1), calibrated prob should differ
        # from raw prob.  Both should be valid probabilities.
        self.assertGreater(prob_raw, 0.0)
        self.assertLess(prob_raw, 1.0)
        self.assertGreater(prob_cal, 0.0)
        self.assertLess(prob_cal, 1.0)
        self.assertNotAlmostEqual(
            prob_raw, prob_cal, places=3,
            msg="Calibration had no effect: probOver unchanged with stat='pts'",
        )

        # Temperature > 1 shrinks toward 0.5, so calibrated prob should be
        # closer to 0.5 than raw.
        self.assertLess(
            abs(prob_cal - 0.5), abs(prob_raw - 0.5),
            msg="Calibrated prob should be closer to 0.5 than raw",
        )

        # Raw values should also be preserved for UI visualization.
        self.assertIn("probOverRaw", result_cal)
        self.assertAlmostEqual(
            result_cal["probOverRaw"], prob_raw, places=3,
            msg="probOverRaw should match uncalibrated probOver",
        )

    # ------------------------------------------------------------------
    # 2. EV output has all fields required by gate_check / _qualifies
    # ------------------------------------------------------------------
    def test_ev_output_feeds_gate_check(self):
        """
        Compute EV for a pts prop with strong edge, then verify the output
        dict has every field that _qualifies() reads from prop_result["ev"].

        Uses projection=50 vs line=24.5 to ensure the calibrated probOver
        (after temperature scaling with pts T up to ~3.3) still lands in bin 9.

        Fields consumed by _qualifies():
          ev.over.edge, ev.under.edge, ev.probOver, ev.probUnder
        """
        # projection=50, line=24.5, stdev=6 -> raw probOver ~0.99999
        # After aggressive calibration (T=3.29), probOver stays in bin 9 (>= 0.90).
        result = compute_ev(
            projection=50.0,
            line=24.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.0,
            stat="pts",
        )
        self.assertIsNotNone(result)

        # Top-level keys that _qualifies reads from ev sub-dict.
        self.assertIn("probOver", result)
        self.assertIn("probUnder", result)
        self.assertIn("over", result)
        self.assertIn("under", result)
        self.assertIn("distributionMode", result)

        # Side sub-dicts must contain edge.
        self.assertIn("edge", result["over"])
        self.assertIn("edge", result["under"])

        # Additional keys consumed downstream (verdict, kelly, etc.).
        self.assertIn("verdict", result["over"])
        self.assertIn("verdict", result["under"])
        self.assertIn("kellyFraction", result["over"])
        self.assertIn("kellyFraction", result["under"])

        # Probability values should be valid.
        self.assertGreaterEqual(result["probOver"], 0.0)
        self.assertLessEqual(result["probOver"], 1.0)
        self.assertGreaterEqual(result["probUnder"], 0.0)
        self.assertLessEqual(result["probUnder"], 1.0)

        # With projection=50 vs line=24.5, over edge should be strongly positive.
        self.assertGreater(result["over"]["edge"], 0.08,
                           "Strong over edge expected for proj=50 vs line=24.5")

        # Calibrated probOver should be in bin 9 for this extreme gap.
        prob_over = result["probOver"]
        bin_idx = max(0, min(9, int(prob_over * 10)))
        self.assertEqual(bin_idx, 9,
                         f"Expected bin 9 for proj=50 vs line=24.5, got bin {bin_idx} "
                         f"(probOver={prob_over:.4f})")

        # Now verify that the wrapped result actually passes _qualifies().
        prop_result = _wrap_ev_for_gate(result)
        ok, reason = _qualifies(prop_result, stat="pts")
        # This prop has high probOver (bin 9), strong edge, high confidence.
        # Without referenceBook, Pinnacle gate is skipped.
        # Should pass all gates.
        self.assertTrue(ok, f"Expected qualifying signal but got: {reason}")
        self.assertEqual(reason, "")

    # ------------------------------------------------------------------
    # 3. Poisson distribution for stl
    # ------------------------------------------------------------------
    def test_poisson_stat_uses_poisson_distribution(self):
        """
        Compute EV for stl stat.  The engine should select the Poisson
        distribution path and report distributionMode == 'poisson'.
        """
        result = compute_ev(
            projection=1.5,
            line=1.5,
            over_odds=-110,
            under_odds=-110,
            stdev=1.0,
            stat="stl",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["distributionMode"], "poisson",
                         "stl must use Poisson distribution")

        # Probabilities should still be valid.
        self.assertGreater(result["probOver"], 0.0)
        self.assertLess(result["probOver"], 1.0)
        total = result["probOver"] + result["probUnder"] + result.get("probPush", 0.0)
        self.assertAlmostEqual(total, 1.0, places=3,
                               msg="Poisson probabilities should sum to 1.0")

    # ------------------------------------------------------------------
    # 4. Normal distribution for pts
    # ------------------------------------------------------------------
    def test_normal_stat_uses_normal_distribution(self):
        """
        Compute EV for pts stat.  The engine should select the Normal CDF
        distribution path and report distributionMode == 'normal'.
        """
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["distributionMode"], "normal",
                         "pts must use Normal distribution")

        total = result["probOver"] + result["probUnder"] + result.get("probPush", 0.0)
        self.assertAlmostEqual(total, 1.0, places=3,
                               msg="Normal probabilities should sum to 1.0")

    # ------------------------------------------------------------------
    # 5. Blocked bin filtered by gate
    # ------------------------------------------------------------------
    def test_blocked_bin_filtered_by_gate(self):
        """
        Compute EV for a prop that lands in a blocked bin (probOver in the
        mid-range, e.g. bin 4-5 where probOver ~ 0.4-0.5), then verify
        _qualifies() blocks it.

        Current policy: BLOCKED_PROB_BINS = {1,2,3,4,5,6,7,8}, so only
        bins 0 and 9 pass.  A projection near the line produces probOver
        near 0.5 (bin 4 or 5), which must be blocked.
        """
        # Projection very close to line -> probOver near 0.5 -> blocked bin.
        result = compute_ev(
            projection=25.5,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.0,
            stat="pts",
        )
        self.assertIsNotNone(result)

        prob_over = result["probOver"]
        bin_idx = max(0, min(9, int(prob_over * 10)))

        # The prop should land in a blocked bin (likely bin 4 or 5 due to
        # calibration pulling toward 0.5, but any blocked bin suffices).
        self.assertIn(bin_idx, BLOCKED_PROB_BINS,
                      f"Expected probOver={prob_over:.4f} (bin {bin_idx}) to be in "
                      f"blocked bins {BLOCKED_PROB_BINS}")

        # Verify the gate blocks it.
        prop_result = _wrap_ev_for_gate(result)
        ok, reason = _qualifies(prop_result, stat="pts")
        self.assertFalse(ok,
                         f"Signal in blocked bin {bin_idx} should be rejected")
        # The reason could be confidence_too_low, blocked_prob_bin, or
        # edge_too_low depending on exact calibrated probability.
        # All are correct blocking behaviors for mid-range props.
        self.assertTrue(
            "blocked_prob_bin" in reason
            or "confidence_too_low" in reason
            or "edge_too_low" in reason,
            f"Expected blocked_prob_bin, confidence_too_low, or edge_too_low; "
            f"got: {reason}",
        )

    # ------------------------------------------------------------------
    # 6. End-to-end: strong pts UNDER signal passes all gates
    # ------------------------------------------------------------------
    def test_strong_under_signal_passes_all_gates(self):
        """
        End-to-end: projection far below the line (pts, 10.0 vs line 25.5)
        should produce a bin-0 UNDER signal with sufficient edge and
        confidence to pass every gate in _qualifies().

        Uses an extreme gap to ensure that even after calibration temperature
        scaling (pts T up to ~3.3), probOver stays below 0.10 (bin 0).

        This is the canonical 'bin 0 UNDER on pts' signal that drives the
        production edge profile.
        """
        # projection=5, line=25.5, stdev=4 -> raw probOver ~0.0000001
        # Even with aggressive temperature compression (T=3.29), probOver
        # stays well below 0.10 (bin 0).
        result = compute_ev(
            projection=5.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=4.0,
            stat="pts",
        )
        self.assertIsNotNone(result)

        # probOver should be very low (< 0.10) -> bin 0.
        prob_over = result["probOver"]
        self.assertLess(prob_over, 0.10,
                        f"Expected low probOver for proj=5 vs line=25.5, got {prob_over}")
        bin_idx = max(0, min(9, int(prob_over * 10)))
        self.assertEqual(bin_idx, 0, f"Expected bin 0, got bin {bin_idx}")

        # Under edge should be strongly positive (projection far below line).
        under_edge = result["under"]["edge"]
        self.assertGreater(under_edge, 0.08,
                           f"Expected strong under edge, got {under_edge}")

        # Confidence (max of probOver, probUnder) should exceed min_confidence.
        confidence = max(prob_over, result["probUnder"])
        self.assertGreaterEqual(confidence, 0.60,
                                f"Expected confidence >= 0.60, got {confidence}")

        # Feed through _qualifies() -- should pass all gates.
        prop_result = _wrap_ev_for_gate(result)
        ok, reason = _qualifies(prop_result, stat="pts")
        self.assertTrue(ok, f"Strong UNDER pts signal should qualify; blocked by: {reason}")
        self.assertEqual(reason, "")

    # ------------------------------------------------------------------
    # 7. Ineligible stat blocked despite strong EV edge
    # ------------------------------------------------------------------
    def test_ineligible_stat_blocked_despite_strong_edge(self):
        """
        Even when compute_ev() produces a strong Poisson edge for blk
        (projection=3.0, line=0.5), _qualifies() must reject it because
        blk is not in ELIGIBLE_STATS.

        This validates the gate layer correctly enforces policy boundaries
        regardless of EV engine output quality.
        """
        result = compute_ev(
            projection=3.0,
            line=0.5,
            over_odds=-110,
            under_odds=-110,
            stdev=1.5,
            stat="blk",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["distributionMode"], "poisson")

        # The EV engine should produce a large over edge.
        over_edge = result["over"]["edge"]
        self.assertGreater(over_edge, 0.10,
                           f"Expected strong over edge for blk proj=3 vs line=0.5, "
                           f"got {over_edge}")

        # But the gate should block it because blk is not eligible.
        prop_result = _wrap_ev_for_gate(result)
        ok, reason = _qualifies(prop_result, stat="blk")
        self.assertFalse(ok, "blk should be rejected as ineligible stat")
        self.assertIn("stat_not_eligible", reason)


# ===========================================================================
# Odds -> CLV -> Gates pipeline
# ===========================================================================

class TestOddsCLVPipeline(unittest.TestCase):
    """Integration tests for OddsStore -> CLV -> Gates pipeline."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_sqlite(self):
        """Skip the test if the odds SQLite file is missing."""
        if not os.path.isfile(_ODDS_DB_PATH):
            self.skipTest(f"Odds SQLite not found: {_ODDS_DB_PATH}")

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_odds_store_loads_without_error(self):
        """OddsStore can be instantiated (creates DB if missing)."""
        from core.nba_odds_store import OddsStore

        # Use a temporary in-memory path if the real file is absent,
        # just to verify the constructor and schema init work.
        import tempfile

        tmp_path = os.path.join(tempfile.gettempdir(), "_test_odds_store.sqlite")
        try:
            store = OddsStore(db_path=tmp_path)
            self.assertIsNotNone(store)
        finally:
            try:
                store.close()
                os.remove(tmp_path)
            except Exception:
                pass

    def test_closing_line_lookup_returns_dict_or_none(self):
        """get_closing_line() returns a dict with expected keys or None."""
        self._require_sqlite()
        from core.nba_odds_store import OddsStore

        store = OddsStore(db_path=_ODDS_DB_PATH)
        try:
            # Use a plausible but potentially nonexistent combo — the test
            # validates the return *type*, not specific data.
            result = store.get_closing_line(
                event_id="nonexistent_event_12345",
                market="player_points",
                player_name="Anthony Edwards",
            )
            if result is not None:
                self.assertIsInstance(result, dict)
                for key in ("book", "close_line", "close_over_odds",
                            "close_under_odds", "close_ts_utc"):
                    self.assertIn(key, result, f"Missing key: {key}")
                self.assertIsInstance(result["close_line"], (int, float))
            else:
                # None is an acceptable return when no data matches
                self.assertIsNone(result)

            # Also try the by-date variant with a real-ish date
            result2 = store.get_closing_line_by_player_date(
                player_name="Anthony Edwards",
                market="player_points",
                date_str="2026-02-01",
            )
            if result2 is not None:
                self.assertIsInstance(result2, dict)
                self.assertIn("close_line", result2)
        finally:
            store.close()

    def test_clv_calculation_positive_means_beat_close(self):
        """
        CLV (closing line value) sanity check: if a pick is taken at
        line=24.5 OVER and the market closes at 25.5, the bettor got a
        lower line on an over — that is positive CLV.
        """
        pick_line = 24.5
        close_line = 25.5
        side = "over"

        # For an OVER bet, CLV is positive when close_line > pick_line
        # (the market moved away from our bet = we got a better number).
        # For UNDER, it is the reverse.
        if side == "over":
            clv_line = close_line - pick_line
        else:
            clv_line = pick_line - close_line

        self.assertGreater(clv_line, 0,
                           "OVER pick at 24.5 closing at 25.5 should be positive CLV")

        # Reverse scenario: pick OVER at 26.5, closes at 25.5 -> negative CLV
        pick_line_bad = 26.5
        clv_line_bad = close_line - pick_line_bad
        self.assertLess(clv_line_bad, 0,
                        "OVER pick at 26.5 closing at 25.5 should be negative CLV")

        # UNDER scenario: pick UNDER at 25.5, closes at 24.5 -> positive CLV
        clv_under = 25.5 - 24.5  # pick_line - close_line for under
        self.assertGreater(clv_under, 0,
                           "UNDER pick at 25.5 closing at 24.5 should be positive CLV")

    def test_real_line_required_stat_blocked_without_closing(self):
        """
        reb requires a real closing line per REAL_LINE_REQUIRED_STATS.
        If used_real_line is False/None, the gate must block it.
        """
        from core.gates import _qualifies

        # Build a prop_result that would otherwise qualify: high edge,
        # high confidence, extreme bin (0 or 9).
        prop_result = {
            "ev": {
                "over":  {"edge": 0.15},
                "under": {"edge": 0.02},
                "probOver":  0.95,   # bin 9
                "probUnder": 0.05,
            },
        }

        # Without real line -> should be blocked for reb
        qualifies, reason = _qualifies(prop_result, "reb", used_real_line=False)
        self.assertFalse(qualifies, "reb without real line should be blocked")
        self.assertIn("real_line_required", reason)

        # With real line -> should NOT be blocked by this gate
        # (may be blocked by other gates, but not by real_line_required)
        qualifies2, reason2 = _qualifies(prop_result, "reb", used_real_line=True)
        self.assertNotIn("real_line_required", reason2,
                         "reb with real line should not fail the real-line gate")

        # pts is NOT in real_line_required_stats -> should pass regardless
        qualifies3, reason3 = _qualifies(prop_result, "pts", used_real_line=False)
        self.assertNotIn("real_line_required", reason3,
                         "pts should never be blocked by real_line_required gate")

    def test_coverage_summary_has_expected_keys(self):
        """coverage_summary() returns a dict with the documented structure."""
        self._require_sqlite()
        from core.nba_odds_store import OddsStore

        store = OddsStore(db_path=_ODDS_DB_PATH)
        try:
            summary = store.coverage_summary()
            self.assertIsInstance(summary, dict)
            expected_keys = {
                "success", "dbPath", "snapshotCount", "closingCount",
                "runCount", "eventCount", "playerCount",
                "dateFrom", "dateTo", "books", "markets", "statKeys",
            }
            for key in expected_keys:
                self.assertIn(key, summary, f"Missing key in coverage_summary: {key}")

            self.assertTrue(summary["success"])
            self.assertIsInstance(summary["snapshotCount"], int)
            self.assertIsInstance(summary["closingCount"], int)
            self.assertIsInstance(summary["books"], list)
            self.assertIsInstance(summary["markets"], list)
            self.assertIsInstance(summary["statKeys"], list)

            # If there is data, verify counts are non-negative
            self.assertGreaterEqual(summary["snapshotCount"], 0)
            self.assertGreaterEqual(summary["closingCount"], 0)
        finally:
            store.close()


# ===========================================================================
# Minutes -> Injury -> Gates pipeline
# ===========================================================================

from core.nba_minutes_model import (
    _MULTIPLIER_ABSOLUTE_MIN,
    _MULTIPLIER_MAX,
    compute_minutes_multiplier,
)


def _make_rolling(avg5=30.0, avg10=30.0, avg_season=30.0, stdev=3.0):
    """Build a minimal rolling-stats dict matching get_player_game_log output."""
    return {
        "min_avg5": avg5,
        "min_avg10": avg10,
        "min_avg_season": avg_season,
        "min_stdev": stdev,
    }


def _make_logs(n=10, minutes=30.0):
    """Build a list of fake game-log entries (most-recent-first)."""
    return [{"min": minutes, "gameDate": f"2026-02-{28 - i:02d}"} for i in range(n)]


class TestMinutesInjuryPipeline(unittest.TestCase):
    """Integration tests for compute_minutes_multiplier + injury caps + gate checks."""

    # -----------------------------------------------------------------
    # 1. Multiplier stays within documented bounds for healthy players
    # -----------------------------------------------------------------
    def test_minutes_multiplier_range(self):
        """compute_minutes_multiplier() output is always within [0.50, 1.15]."""
        scenarios = [
            # standard starter
            (_make_rolling(32, 31, 30, 2.5), _make_logs(15, 32), False),
            # bench player with high variance
            (_make_rolling(12, 14, 13, 5.0), _make_logs(8, 12), False),
            # back-to-back game
            (_make_rolling(28, 28, 28, 3.0), _make_logs(10, 28), True),
            # very small sample
            (_make_rolling(25, 25, 25, 4.0), _make_logs(3, 25), False),
        ]
        for rolling, logs, b2b in scenarios:
            result = compute_minutes_multiplier(rolling, logs, is_b2b=b2b)
            mult = result["multiplier"]
            conf = result["minutesConfidence"]
            with self.subTest(avg_s=rolling["min_avg_season"], b2b=b2b):
                self.assertGreaterEqual(mult, _MULTIPLIER_ABSOLUTE_MIN)
                self.assertLessEqual(mult, _MULTIPLIER_MAX)
                self.assertGreaterEqual(conf, 0.10)
                self.assertLessEqual(conf, 0.95)
                self.assertIsInstance(result["minutesReasoning"], list)

    # -----------------------------------------------------------------
    # 2. Injury-return cap reduces multiplier below healthy baseline
    # -----------------------------------------------------------------
    def test_minutes_multiplier_with_injury_reduces(self):
        """A player returning from 6+ consecutive DNPs gets a lower multiplier
        than the same player without injury context."""
        rolling = _make_rolling(30, 30, 30, 3.0)
        logs = _make_logs(10, 30)

        # Healthy baseline (no excluded games)
        healthy = compute_minutes_multiplier(rolling, logs, excluded_games=None)

        # Injured: 9 DNPs immediately before the most recent game.
        # Create logs with a gap: most recent Feb 28, second Feb 18, rest before.
        gap_logs = [
            {"min": 30, "gameDate": "2026-02-28"},
            {"min": 30, "gameDate": "2026-02-18"},
        ] + [{"min": 30, "gameDate": f"2026-02-{17 - i:02d}"} for i in range(8)]

        dnp_games = [
            {"gameDate": f"2026-02-{d:02d}", "gameId": f"DNP{d}"}
            for d in range(19, 28)   # Feb 19..27 = 9 DNPs between Feb 18 and Feb 28
        ]
        injured = compute_minutes_multiplier(rolling, gap_logs, excluded_games=dnp_games)

        self.assertLess(injured["multiplier"], healthy["multiplier"],
                        "Injury-return cap should reduce multiplier below healthy baseline")
        self.assertLess(injured["minutesConfidence"], healthy["minutesConfidence"],
                        "Injury-return should reduce confidence")
        # At least one reasoning tag should mention injury_return
        injury_tags = [t for t in injured["minutesReasoning"] if "injury_return" in t]
        self.assertTrue(injury_tags, "Missing injury_return reasoning tag")

    # -----------------------------------------------------------------
    # 3. Determinism: same inputs always produce the same output
    # -----------------------------------------------------------------
    def test_minutes_multiplier_deterministic(self):
        """Identical inputs must yield identical outputs across repeated calls."""
        rolling = _make_rolling(28, 27, 28, 2.8)
        logs = _make_logs(12, 28)
        results = [
            compute_minutes_multiplier(rolling, logs, is_b2b=False)
            for _ in range(5)
        ]
        for i in range(1, len(results)):
            self.assertEqual(results[0]["multiplier"], results[i]["multiplier"])
            self.assertEqual(results[0]["minutesConfidence"], results[i]["minutesConfidence"])
            self.assertEqual(results[0]["minutesReasoning"], results[i]["minutesReasoning"])

    # -----------------------------------------------------------------
    # 4. Extreme / degenerate inputs do not crash or produce NaN
    # -----------------------------------------------------------------
    def test_extreme_minutes_inputs(self):
        """Very high, zero, and negative minutes must not crash or return NaN/Inf."""
        edge_cases = [
            # zero across the board
            _make_rolling(0, 0, 0, 0),
            # extremely high minutes (overtime-heavy)
            _make_rolling(55, 50, 48, 8.0),
            # stdev larger than mean (pathological)
            _make_rolling(10, 10, 10, 20.0),
            # negative values (should not exist, but must not crash)
            _make_rolling(-5, -3, -2, 1.0),
        ]
        for rolling in edge_cases:
            with self.subTest(avg_s=rolling["min_avg_season"]):
                result = compute_minutes_multiplier(rolling, _make_logs(2, 5))
                self.assertFalse(math.isnan(result["multiplier"]))
                self.assertFalse(math.isinf(result["multiplier"]))
                self.assertFalse(math.isnan(result["minutesConfidence"]))
                self.assertFalse(math.isinf(result["minutesConfidence"]))
                self.assertGreaterEqual(result["multiplier"], _MULTIPLIER_ABSOLUTE_MIN)
                self.assertLessEqual(result["multiplier"], _MULTIPLIER_MAX)

        # Empty logs
        result = compute_minutes_multiplier(_make_rolling(), [], is_b2b=True)
        self.assertFalse(math.isnan(result["multiplier"]))

        # None-ish rolling values
        result = compute_minutes_multiplier(
            {"min_avg5": None, "min_avg10": None, "min_avg_season": None, "min_stdev": None},
            _make_logs(5, 20),
        )
        self.assertFalse(math.isnan(result["multiplier"]))

    # -----------------------------------------------------------------
    # 5. Injury-cap table applies for various DNP streak lengths
    # -----------------------------------------------------------------
    def test_minutes_cap_applied_for_injury_return_scenarios(self):
        """Verify injury-return caps from _INJURY_CAP_TABLE are applied for
        players returning from different DNP streak lengths."""
        # Build logs where the most recent game is Feb 28.
        # The second-most-recent game is Feb 18 (leaves a gap for DNPs).
        logs = [
            {"min": 30, "gameDate": "2026-02-28"},   # most recent
            {"min": 30, "gameDate": "2026-02-18"},   # second most recent
        ] + [{"min": 30, "gameDate": f"2026-02-{17 - i:02d}"} for i in range(8)]

        rolling = _make_rolling(30, 30, 30, 3.0)

        # Case A: 9 DNPs (>=6), games_since_return=1 => cap=0.65
        dnps_many = [
            {"gameDate": f"2026-02-{d:02d}", "gameId": f"DNP{d}"}
            for d in range(19, 28)   # Feb 19..27 = 9 DNPs
        ]
        result_many = compute_minutes_multiplier(rolling, logs, excluded_games=dnps_many)
        self.assertLessEqual(result_many["multiplier"], 0.65,
                             "6+ DNPs with 1 game back should cap at 0.65")

        # Case B: 3 DNPs, games_since_return=1 => cap=0.72
        dnps_3 = [
            {"gameDate": f"2026-02-{d:02d}", "gameId": f"DNP{d}"}
            for d in range(25, 28)   # Feb 25..27 = 3 DNPs
        ]
        result_3 = compute_minutes_multiplier(rolling, logs, excluded_games=dnps_3)
        self.assertLessEqual(result_3["multiplier"], 0.72,
                             "3 DNPs with 1 game back should cap at 0.72")

        # Case C: 1 DNP, games_since_return=1 => cap=0.82
        dnps_1 = [
            {"gameDate": "2026-02-27", "gameId": "DNP27"}
        ]
        result_1 = compute_minutes_multiplier(rolling, logs, excluded_games=dnps_1)
        self.assertLessEqual(result_1["multiplier"], 0.82,
                             "1 DNP with 1 game back should cap at 0.82")

        # All injury scenarios should be strictly below 1.0
        for label, res in [("9dnp", result_many), ("3dnp", result_3), ("1dnp", result_1)]:
            with self.subTest(scenario=label):
                self.assertLess(res["multiplier"], 1.0)
                injury_tags = [t for t in res["minutesReasoning"] if "injury_return" in t]
                self.assertTrue(injury_tags, f"{label}: missing injury_return tag")


if __name__ == "__main__":
    unittest.main()
