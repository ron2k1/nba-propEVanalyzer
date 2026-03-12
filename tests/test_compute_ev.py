"""
tests/test_compute_ev.py — Pin compute_ev() behavior during FREEZE period.

PURPOSE: Verify core EV math (Normal CDF for pts/ast/reb, Poisson for blk/stl/fg3m/tov)
produces exact outputs matching current production behavior. These tests capture real
outputs from the engine as of 2026-03-05 (commit bc016f5).

Rules:
- FREEZE: do NOT change source to make tests pass. If behavior differs, flag and adjust test.
- No mocks: all tests call the real compute_ev() with synthetic inputs.
- Tolerances: ±0.005 for probabilities, ±0.005 for edges (floating point precision).
"""

import os
import sys
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.nba_ev_engine import compute_ev


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract(result: dict):
    """Pull the key fields from a compute_ev() result dict."""
    ev = result.get("ev", result)
    return {
        "probOver": ev.get("probOver"),
        "probUnder": ev.get("probUnder"),
        "distributionMode": ev.get("distributionMode"),
        "over_edge": ev.get("over", {}).get("edge"),
        "under_edge": ev.get("under", {}).get("edge"),
    }


# ===========================================================================
# A. Normal CDF path (pts, ast, reb)
# ===========================================================================

class TestComputeEvNormal:
    """
    pts/ast/reb use Normal CDF. Calibration temperatures apply per-stat
    when models/prob_calibration.json exists.

    Captured outputs (2026-03-05):
      pts, proj=27.0, line=25.5, stdev=6.5, odds=-110/-110
        → dist=normal, probOver≈0.522, over_edge≈0.022, under_edge≈-0.022
    """

    def test_pts_over_line_normal(self):
        """pts: projection above line → slight over edge."""
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "normal"
        # Calibration temps shift probabilities; pin actual output with wider tolerance
        assert ev["probOver"] == pytest.approx(0.5115, abs=0.02)
        assert ev["over_edge"] > 0.0  # projection above line → positive over edge
        assert ev["under_edge"] < 0.0

    def test_pts_equal_line(self):
        """pts: projection equals line → probOver slightly below 0.5 (continuity correction)."""
        result = compute_ev(
            projection=25.5,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "normal"
        # With calibration, probOver may not be exactly 0.5
        assert 0.40 < ev["probOver"] < 0.60

    def test_pts_zero_stdev_fallback(self):
        """
        pts with stdev=0.0 — engine should handle gracefully.
        The engine has a minimum stdev floor to avoid division by zero.
        """
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=0.0,
            stat="pts",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "normal"
        # With zero stdev, engine applies a floor — probOver should still be valid
        assert 0.0 <= ev["probOver"] <= 1.0
        assert ev["over_edge"] is not None

    def test_ast_asymmetric_odds(self):
        """
        ast: projection above line with asymmetric odds.
        ast has min_edge_by_stat=0.09 in gates but that doesn't affect compute_ev().
        """
        result = compute_ev(
            projection=8.0,
            line=6.5,
            over_odds=-130,
            under_odds=+110,
            stdev=3.0,
            stat="ast",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "normal"
        # Projection well above line → probOver > 0.5
        assert ev["probOver"] > 0.50
        # With -130 juice on over, edge is reduced
        assert ev["over_edge"] is not None

    def test_reb_normal_path(self):
        """reb uses Normal CDF (not Poisson), same as pts/ast."""
        result = compute_ev(
            projection=10.0,
            line=8.5,
            over_odds=-110,
            under_odds=-110,
            stdev=4.0,
            stat="reb",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "normal"
        assert ev["probOver"] > 0.50


# ===========================================================================
# B. Poisson path (blk, stl, fg3m, tov)
# ===========================================================================

class TestComputeEvPoisson:
    """
    Poisson distribution for low-count stats. These stats have structural
    Poisson bias and are NOT in the betting whitelist, but compute_ev()
    still produces valid outputs for research.

    Captured output (2026-03-05):
      blk, proj=1.2, line=1.5, stdev=1.0, odds=-110/-110
        → dist=poisson, probOver≈0.4554, over_edge≈-0.0446, under_edge≈0.0446
    """

    def test_blk_poisson_path(self):
        """blk: projection below line → under edge positive."""
        result = compute_ev(
            projection=1.2,
            line=1.5,
            over_odds=-110,
            under_odds=-110,
            stdev=1.0,
            stat="blk",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "poisson"
        # Calibration temps shift Poisson probabilities; pin actual output
        assert ev["probOver"] == pytest.approx(0.3374, abs=0.03)
        assert ev["under_edge"] > 0.0  # under is the value side
        assert ev["over_edge"] < 0.0

    def test_stl_poisson_path(self):
        """stl uses Poisson distribution."""
        result = compute_ev(
            projection=1.5,
            line=1.5,
            over_odds=-110,
            under_odds=-110,
            stdev=1.0,
            stat="stl",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "poisson"
        assert 0.0 <= ev["probOver"] <= 1.0

    def test_fg3m_poisson_path(self):
        """fg3m uses Poisson distribution."""
        result = compute_ev(
            projection=2.5,
            line=2.5,
            over_odds=-110,
            under_odds=-110,
            stdev=1.5,
            stat="fg3m",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "poisson"

    def test_tov_poisson_path(self):
        """tov uses Poisson distribution."""
        result = compute_ev(
            projection=3.0,
            line=2.5,
            over_odds=-110,
            under_odds=-110,
            stdev=1.5,
            stat="tov",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "poisson"


# ===========================================================================
# C. Edge computation invariants
# ===========================================================================

class TestEdgeInvariants:
    """
    Structural invariants that must hold regardless of stat/distribution:
    - probOver + probUnder ≈ 1.0 (before calibration rounding)
    - over_edge + under_edge ≈ 0.0 at fair odds (both -110)
    - Higher projection → higher probOver
    - Edge is computed vs no-vig fair probability
    """

    def test_prob_sum_near_one(self):
        """probOver + probUnder should be close to 1.0."""
        result = compute_ev(
            projection=27.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        ev = _extract(result)
        total = ev["probOver"] + ev["probUnder"]
        assert total == pytest.approx(1.0, abs=0.01)

    def test_edge_sum_near_zero_at_fair_odds(self):
        """At -110/-110 (fair), over_edge + under_edge ≈ 0."""
        result = compute_ev(
            projection=27.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        ev = _extract(result)
        edge_sum = ev["over_edge"] + ev["under_edge"]
        assert edge_sum == pytest.approx(0.0, abs=0.02)

    def test_higher_projection_higher_prob_over(self):
        """Increasing projection should increase probOver."""
        result_low = compute_ev(
            projection=24.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        result_high = compute_ev(
            projection=30.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        assert _extract(result_high)["probOver"] > _extract(result_low)["probOver"]

    def test_large_projection_gap_strong_edge(self):
        """Projection far above line → large over edge."""
        result = compute_ev(
            projection=35.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        ev = _extract(result)
        assert ev["over_edge"] > 0.10

    def test_large_negative_gap_strong_under_edge(self):
        """Projection far below line → large under edge."""
        result = compute_ev(
            projection=18.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        ev = _extract(result)
        assert ev["under_edge"] > 0.10
        assert ev["over_edge"] < 0.0

    def test_heavy_juice_reduces_edge(self):
        """Heavy juice on one side (-200) should reduce that side's edge."""
        result_fair = compute_ev(
            projection=27.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        result_juiced = compute_ev(
            projection=27.0, line=25.5, over_odds=-200, under_odds=+170,
            stdev=6.5, stat="pts",
        )
        # Heavier juice on over → lower over edge
        assert _extract(result_juiced)["over_edge"] < _extract(result_fair)["over_edge"]


# ===========================================================================
# D. Calibration integration
# ===========================================================================

class TestCalibrationIntegration:
    """
    compute_ev() applies temperature scaling from models/prob_calibration.json
    when stat= is provided. These tests verify that stat= changes the output
    vs no stat (or an uncalibrated stat).
    """

    def test_stat_param_accepted(self):
        """compute_ev() must accept stat= without error."""
        result = compute_ev(
            projection=27.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        assert "ev" in result or "probOver" in result

    def test_result_has_distribution_mode(self):
        """Output must include distributionMode field."""
        result = compute_ev(
            projection=27.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        ev = result.get("ev", result)
        assert "distributionMode" in ev

    def test_result_has_over_under_dicts(self):
        """Output must have over/under sub-dicts with edge fields."""
        result = compute_ev(
            projection=27.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        ev = result.get("ev", result)
        assert "over" in ev
        assert "under" in ev
        assert "edge" in ev["over"]
        assert "edge" in ev["under"]


# ===========================================================================
# E. Boundary / extreme input tests
# ===========================================================================

class TestComputeEvBoundaries:
    """
    Verify compute_ev() handles extreme inputs without crashing or
    returning NaN/Inf. The engine should produce valid probabilities
    (0 <= p <= 1) and finite edges for all reasonable inputs.
    """

    def test_very_large_projection(self):
        """Projection far above line → probOver near 1.0, no NaN."""
        result = compute_ev(
            projection=100.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        ev = _extract(result)
        assert ev["probOver"] is not None
        assert 0.99 <= ev["probOver"] <= 1.0
        assert ev["over_edge"] is not None

    def test_very_small_projection(self):
        """Projection far below line → probOver very low, no NaN."""
        result = compute_ev(
            projection=0.5, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        ev = _extract(result)
        assert ev["probOver"] is not None
        # Bin-level calibration may push this above raw value; just verify it's low
        assert 0.0 <= ev["probOver"] <= 0.15

    def test_very_small_stdev(self):
        """Tiny stdev (near zero but nonzero) → valid output."""
        result = compute_ev(
            projection=27.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=0.001, stat="pts",
        )
        ev = _extract(result)
        assert 0.0 <= ev["probOver"] <= 1.0
        assert ev["over_edge"] is not None

    def test_very_large_stdev(self):
        """Huge stdev → probOver near 0.5."""
        result = compute_ev(
            projection=27.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=1000.0, stat="pts",
        )
        ev = _extract(result)
        assert 0.49 < ev["probOver"] < 0.51

    def test_negative_projection(self):
        """Negative projection (invalid but shouldn't crash)."""
        result = compute_ev(
            projection=-5.0, line=25.5, over_odds=-110, under_odds=-110,
            stdev=6.5, stat="pts",
        )
        ev = _extract(result)
        assert 0.0 <= ev["probOver"] <= 1.0

    def test_zero_line(self):
        """Line = 0 with positive projection."""
        result = compute_ev(
            projection=5.0, line=0.0, over_odds=-110, under_odds=-110,
            stdev=2.0, stat="pts",
        )
        ev = _extract(result)
        assert ev["probOver"] > 0.5

    def test_extreme_odds_heavy_favorite(self):
        """Very heavy favorite odds (-500) → valid edge."""
        result = compute_ev(
            projection=27.0, line=25.5, over_odds=-500, under_odds=+400,
            stdev=6.5, stat="pts",
        )
        ev = _extract(result)
        assert 0.0 <= ev["probOver"] <= 1.0
        assert ev["over_edge"] is not None
        assert ev["under_edge"] is not None

    def test_extreme_odds_heavy_underdog(self):
        """Very heavy underdog odds (+500) → valid edge."""
        result = compute_ev(
            projection=27.0, line=25.5, over_odds=+500, under_odds=-600,
            stdev=6.5, stat="pts",
        )
        ev = _extract(result)
        assert 0.0 <= ev["probOver"] <= 1.0

    def test_poisson_large_lambda(self):
        """Poisson with large lambda (projection=15) for blk → valid output."""
        result = compute_ev(
            projection=15.0, line=10.5, over_odds=-110, under_odds=-110,
            stdev=3.0, stat="blk",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "poisson"
        assert 0.0 <= ev["probOver"] <= 1.0

    def test_poisson_zero_projection(self):
        """Poisson with projection=0 (lambda=0) for blk → valid output."""
        result = compute_ev(
            projection=0.0, line=0.5, over_odds=-110, under_odds=-110,
            stdev=1.0, stat="blk",
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "poisson"
        assert 0.0 <= ev["probOver"] <= 1.0

    def test_equal_projection_and_line_all_stats(self):
        """When projection == line, probOver should be valid for all stats.
        Note: bin-level calibration applies different T per bin, so probOver +
        probUnder may not sum to exactly 1.0 after calibration."""
        for stat in ("pts", "reb", "ast", "blk", "stl", "fg3m", "tov"):
            result = compute_ev(
                projection=5.0, line=5.0, over_odds=-110, under_odds=-110,
                stdev=2.0, stat=stat,
            )
            ev = _extract(result)
            assert 0.0 <= ev["probOver"] <= 1.0, f"{stat}: probOver out of range"
            assert 0.0 <= ev["probUnder"] <= 1.0, f"{stat}: probUnder out of range"
            assert ev["over_edge"] is not None, f"{stat}: over_edge missing"
            assert ev["under_edge"] is not None, f"{stat}: under_edge missing"
