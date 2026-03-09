"""
tests/test_ev_reference_mode.py — Pin compute_ev() reference_probs mode.

Tests cover:
- reference_probs={"over": 0.55, "under": 0.45} → distributionMode == "reference"
- probOver/probUnder approximately match reference probs (within normalization)
- Calibration temperature scaling is NOT applied in reference mode
- Edge computed correctly vs no-vig fair probability from reference probs

No network calls. All inputs are synthetic.
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

def _extract(result: dict) -> dict:
    ev = result.get("ev", result)
    return {
        "probOver": ev.get("probOver"),
        "probUnder": ev.get("probUnder"),
        "distributionMode": ev.get("distributionMode"),
        "over_edge": ev.get("over", {}).get("edge"),
        "under_edge": ev.get("under", {}).get("edge"),
    }


# ---------------------------------------------------------------------------
# Test 1: distributionMode == "reference"
# ---------------------------------------------------------------------------

class TestReferenceModeActivated:

    def test_reference_probs_sets_distribution_mode(self):
        """reference_probs parameter → distributionMode must be 'reference'."""
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
            reference_probs={"over": 0.55, "under": 0.45},
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "reference"

    def test_normal_mode_without_reference_probs(self):
        """Without reference_probs, distributionMode should be 'normal' for pts."""
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


# ---------------------------------------------------------------------------
# Test 2: probOver/probUnder match reference probs (within normalization)
# ---------------------------------------------------------------------------

class TestReferenceProbValues:

    def test_prob_over_matches_reference(self):
        """
        reference_probs={"over": 0.55, "under": 0.45}.
        After normalization (total_prob = 0.55+0.45+0=1.0) probOver should ≈ 0.55.
        Calibration is skipped in reference mode, so probOver stays at input value.
        """
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
            reference_probs={"over": 0.55, "under": 0.45},
        )
        ev = _extract(result)
        assert ev["probOver"] == pytest.approx(0.55, abs=0.01)

    def test_prob_under_matches_reference(self):
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
            reference_probs={"over": 0.55, "under": 0.45},
        )
        ev = _extract(result)
        assert ev["probUnder"] == pytest.approx(0.45, abs=0.01)

    def test_prob_sum_near_one(self):
        """probOver + probUnder ≈ 1.0 (push probability absent here)."""
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
            reference_probs={"over": 0.55, "under": 0.45},
        )
        ev = _extract(result)
        assert ev["probOver"] + ev["probUnder"] == pytest.approx(1.0, abs=0.01)

    def test_reference_probs_with_push_component(self):
        """
        reference_probs with push != 0.
        over + under + push = 0.50 + 0.40 + 0.10 = 1.0.
        After normalization: probOver ≈ 0.50, probUnder ≈ 0.40.
        """
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
            reference_probs={"over": 0.50, "under": 0.40, "push": 0.10},
        )
        ev = _extract(result)
        assert ev["probOver"] == pytest.approx(0.50, abs=0.02)
        assert ev["probUnder"] == pytest.approx(0.40, abs=0.02)

    def test_extreme_reference_prob_over(self):
        """reference_probs can be close to 1.0 without error."""
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
            reference_probs={"over": 0.95, "under": 0.05},
        )
        ev = _extract(result)
        assert ev["distributionMode"] == "reference"
        assert ev["probOver"] == pytest.approx(0.95, abs=0.01)


# ---------------------------------------------------------------------------
# Test 3: Edge direction matches reference probs
# ---------------------------------------------------------------------------

class TestReferenceEdgeDirection:

    def test_over_edge_positive_when_prob_over_beats_implied(self):
        """
        reference_probs={"over": 0.65, "under": 0.35}, odds=-110/-110.
        No-vig fair over prob ≈ 0.50.  probOver=0.65 > 0.50 → over_edge > 0.
        """
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
            reference_probs={"over": 0.65, "under": 0.35},
        )
        ev = _extract(result)
        assert ev["over_edge"] > 0.0

    def test_under_edge_positive_when_prob_under_beats_implied(self):
        """
        reference_probs={"over": 0.35, "under": 0.65}.
        probUnder=0.65 > no-vig 0.50 → under_edge > 0.
        """
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
            reference_probs={"over": 0.35, "under": 0.65},
        )
        ev = _extract(result)
        assert ev["under_edge"] > 0.0

    def test_no_edge_when_probs_match_vig(self):
        """
        reference_probs={"over": 0.50, "under": 0.50} at -110/-110.
        No-vig = 0.50. Over/under edge ≈ 0.
        """
        result = compute_ev(
            projection=27.0,
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            stdev=6.5,
            stat="pts",
            reference_probs={"over": 0.50, "under": 0.50},
        )
        ev = _extract(result)
        assert ev["over_edge"] == pytest.approx(0.0, abs=0.02)
        assert ev["under_edge"] == pytest.approx(0.0, abs=0.02)
