"""
tests/test_parlay_engine.py — Pin compute_parlay_ev() behavior.

Tests cover:
- 2-leg independent parlay: jointProb ≈ naive product when correlation=0
- 2-leg same-player correlated parlay (pts+reb): jointProb > naive product
- Invalid odds returns error dict
- Success/failure shape invariants

No network calls. All inputs are synthetic.
"""

import os
import sys
import math
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.nba_parlay_engine import compute_parlay_ev, _joint_prob_2, _get_stat_correlation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _leg(
    prob_over: float,
    stat: str,
    side: str = "over",
    over_odds: int = -110,
    under_odds: int = -110,
    player_id: int = 1,
    player_team: str = "MIN",
    line: float = 25.5,
):
    return {
        "probOver": prob_over,
        "stat": stat,
        "side": side,
        "overOdds": over_odds,
        "underOdds": under_odds,
        "playerId": player_id,
        "playerTeam": player_team,
        "line": line,
    }


# ---------------------------------------------------------------------------
# Test 1: 2-leg independent parlay
# ---------------------------------------------------------------------------

class TestTwoLegIndependent:
    """
    Two legs from different players on different teams → correlation = 0.0.
    _joint_prob_2(p1, p2, rho=0) = p1*p2 + 0 = naive product.
    """

    def test_joint_prob_equals_naive_for_independent_legs(self):
        # Different players, different teams → rho should be 0
        legs = [
            _leg(prob_over=0.6, stat="pts", player_id=1, player_team="MIN"),
            _leg(prob_over=0.6, stat="pts", player_id=2, player_team="LAL"),
        ]
        result = compute_parlay_ev(legs)
        assert result["success"] is True
        # Naive product = 0.6 * 0.6 = 0.36
        naive = result["naiveJointProb"]
        joint = result["jointProb"]
        assert naive == pytest.approx(0.36, abs=0.01)
        # With rho=0, jointProb ≈ naive product
        assert joint == pytest.approx(naive, abs=0.01)

    def test_result_shape(self):
        legs = [
            _leg(prob_over=0.6, stat="pts", player_id=1, player_team="MIN"),
            _leg(prob_over=0.7, stat="ast", player_id=2, player_team="LAL"),
        ]
        result = compute_parlay_ev(legs)
        assert result["success"] is True
        assert "jointProb" in result
        assert "naiveJointProb" in result
        assert "correlationImpact" in result
        assert "evPercent" in result
        assert "verdict" in result
        assert "legs" in result
        assert result["legCount"] == 2

    def test_joint_prob_bounded(self):
        legs = [
            _leg(prob_over=0.6, stat="pts", player_id=1, player_team="MIN"),
            _leg(prob_over=0.6, stat="pts", player_id=2, player_team="LAL"),
        ]
        result = compute_parlay_ev(legs)
        assert 0.0 < result["jointProb"] < 1.0


# ---------------------------------------------------------------------------
# Test 2: 2-leg same-player correlated parlay (pts + reb)
# ---------------------------------------------------------------------------

class TestTwoLegCorrelated:
    """
    Same player, pts + reb. _SAME_PLAYER_CORR[frozenset(["pts","reb"])] = 0.32.
    _joint_prob_2(p, p, rho=0.32) = p^2 + 0.32 * phi(z)^2 > p^2 (naive).
    """

    def test_correlated_joint_prob_exceeds_naive(self):
        # Same player ID and team → rho = 0.32 (pts+reb same-player correlation)
        legs = [
            _leg(prob_over=0.6, stat="pts", player_id=99, player_team="MIN"),
            _leg(prob_over=0.6, stat="reb", player_id=99, player_team="MIN"),
        ]
        result = compute_parlay_ev(legs)
        assert result["success"] is True
        # Positive correlation → joint > naive
        assert result["jointProb"] > result["naiveJointProb"]
        # Correlation impact should be positive (non-trivial for rho=0.32 at p=0.6)
        assert result["correlationImpact"] > 0.0

    def test_same_player_correlation_label(self):
        legs = [
            _leg(prob_over=0.6, stat="pts", player_id=99, player_team="MIN"),
            _leg(prob_over=0.6, stat="reb", player_id=99, player_team="MIN"),
        ]
        result = compute_parlay_ev(legs)
        # Correlation key for legs 1 and 2
        assert "leg1_leg2" in result["correlations"]
        rho = result["correlations"]["leg1_leg2"]
        # pts+reb same-player default = 0.32
        assert rho == pytest.approx(0.32, abs=0.01)


# ---------------------------------------------------------------------------
# Test 3: Invalid odds → error
# ---------------------------------------------------------------------------

class TestInvalidOdds:

    def test_dec_odds_at_exactly_1_returns_error(self):
        # american_to_decimal(0) returns None → should return error
        legs = [
            _leg(prob_over=0.6, stat="pts", over_odds=0),
            _leg(prob_over=0.6, stat="ast"),
        ]
        result = compute_parlay_ev(legs)
        assert result["success"] is False
        assert "error" in result

    def test_too_few_legs(self):
        result = compute_parlay_ev([_leg(prob_over=0.6, stat="pts")])
        assert result["success"] is False
        assert "error" in result

    def test_too_many_legs(self):
        legs = [_leg(prob_over=0.6, stat="pts", player_id=i) for i in range(4)]
        result = compute_parlay_ev(legs)
        assert result["success"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# Test 4: _joint_prob_2 unit tests
# ---------------------------------------------------------------------------

class TestJointProb2:
    """Direct unit tests on the core math function."""

    def test_zero_correlation_equals_product(self):
        p1, p2 = 0.6, 0.6
        joint = _joint_prob_2(p1, p2, rho=0.0)
        assert joint == pytest.approx(p1 * p2, abs=0.005)

    def test_positive_correlation_exceeds_product(self):
        p1, p2 = 0.6, 0.6
        naive = p1 * p2
        joint = _joint_prob_2(p1, p2, rho=0.32)
        assert joint > naive

    def test_negative_correlation_below_product(self):
        p1, p2 = 0.6, 0.6
        naive = p1 * p2
        joint = _joint_prob_2(p1, p2, rho=-0.12)
        assert joint < naive
