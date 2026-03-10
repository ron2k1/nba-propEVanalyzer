"""
tests/test_projection_signals.py — Pin defense-rank modulation, role-change
detection, and ML feature-importance extraction.

Covers untested branches identified in code review:
- _defense_adj rank-weight modulation (nba_prep_projection.py:131)
- Role-change detection logic (nba_prep_projection.py:437)
- _extract_feature_importances (nba_model_ml_training.py:421)

No network calls. All inputs are synthetic.
"""

import os
import sys
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.nba_prep_projection import _defense_adj, _STAT_TO_DEF_RANK, _detect_role_change
from core.nba_model_ml_training import _extract_feature_importances


# ---------------------------------------------------------------------------
# Test 1: Defense rank-weight modulation
# ---------------------------------------------------------------------------

class TestDefenseRankModulation:
    """Verify rank-weight scales multiplier distance from neutral."""

    def _opp_def(self, rank_key=None, rank_val=15, **mults):
        """Build a synthetic opp_def dict with specified rank and multipliers."""
        d = {
            "defPtsMult": 1.0, "defRebMult": 1.0, "defAstMult": 1.0,
            "defFg3mMult": 1.0, "defStlMult": 1.0, "defBlkMult": 1.0,
            "defTovMult": 1.0, "paceFactor": 1.0,
        }
        d.update(mults)
        if rank_key:
            d[rank_key] = rank_val
        return d

    def test_extreme_rank_amplifies_effect(self):
        """Rank 1 (best defense) → rank_weight=1.173, amplifies multiplier."""
        opp = self._opp_def(rank_key="defPtsRank", rank_val=1, defPtsMult=0.90)
        adj_extreme = _defense_adj("pts", opp, "G")

        opp_mid = self._opp_def(rank_key="defPtsRank", rank_val=15, defPtsMult=0.90)
        adj_mid = _defense_adj("pts", opp_mid, "G")

        # Extreme rank should push adj further from 1.0 than middle rank
        assert abs(adj_extreme - 1.0) > abs(adj_mid - 1.0)

    def test_middle_rank_compresses_effect(self):
        """Rank 15 (middle) → rank_weight=0.8, compresses multiplier."""
        # rank_weight = 0.8 + 0.4 * abs(15-15)/15 = 0.8
        opp = self._opp_def(rank_key="defPtsRank", rank_val=15, defPtsMult=1.10)
        adj = _defense_adj("pts", opp, "G")
        # With rank=15, the distance from 1.0 is compressed by 0.8x
        # So if raw adj=1.10, rank-weighted = 1.0 + (1.10-1.0)*0.8 = 1.08
        # (plus clamping, but still closer to 1.0 than raw)
        assert adj < 1.10  # compressed toward 1.0

    def test_rank_30_amplifies_like_rank_1(self):
        """Rank 30 (worst defense) → same rank_weight as rank 1."""
        opp_1 = self._opp_def(rank_key="defPtsRank", rank_val=1, defPtsMult=1.10)
        opp_30 = self._opp_def(rank_key="defPtsRank", rank_val=30, defPtsMult=1.10)
        adj_1 = _defense_adj("pts", opp_1, "G")
        adj_30 = _defense_adj("pts", opp_30, "G")
        # Both have abs(rank-15)=14 or 15, so rank_weight ≈ 1.17-1.2
        # Results should be very close (symmetric around rank 15)
        assert abs(adj_1 - adj_30) < 0.02

    def test_stat_without_rank_key_no_modulation(self):
        """stl has no rank key → no rank modulation applied."""
        assert "stl" not in _STAT_TO_DEF_RANK
        opp = self._opp_def(defStlMult=0.85)
        adj = _defense_adj("stl", opp, "G")
        # Should still return a valid adjustment (just no rank scaling)
        assert 0.70 <= adj <= 1.40

    def test_no_opp_def_returns_neutral(self):
        """Empty/None opp_def → 1.0."""
        assert _defense_adj("pts", None, "G") == 1.0
        assert _defense_adj("pts", {}, "G") == 1.0

    def test_result_clamped_to_config_bounds(self):
        """Result must be within PROJECTION_CONFIG defense_adj bounds (0.70, 1.40)."""
        # Extremely favorable defense + extreme rank
        opp = self._opp_def(rank_key="defPtsRank", rank_val=30,
                            defPtsMult=2.0, paceFactor=1.3)
        adj = _defense_adj("pts", opp, "G")
        assert adj <= 1.40
        # Extremely tough defense + extreme rank
        opp2 = self._opp_def(rank_key="defPtsRank", rank_val=1,
                             defPtsMult=0.3, paceFactor=0.7)
        adj2 = _defense_adj("pts", opp2, "G")
        assert adj2 >= 0.70


# ---------------------------------------------------------------------------
# Test 2: Role-change detection via _detect_role_change()
# ---------------------------------------------------------------------------

class TestRoleChangeDetection:
    """Pin _detect_role_change() — the extracted role-change detection helper."""

    @staticmethod
    def _rolling(season_avg):
        return {"min_avg_season": season_avg}

    @staticmethod
    def _logs(minutes_list):
        return [{"min": m} for m in minutes_list]

    def test_starter_role_change_threshold(self):
        """Starter (32min): threshold = max(3.0, 32*0.15) = 4.8."""
        detected, delta, threshold = _detect_role_change(
            self._rolling(32.0), self._logs([38.0, 39.0, 37.0]))
        assert threshold == pytest.approx(4.8)
        assert detected is True
        assert delta > 0  # role expanded

    def test_bench_player_uses_floor(self):
        """Bench player (15min): threshold = max(3.0, 15*0.15) = 3.0 (floor)."""
        detected, delta, threshold = _detect_role_change(
            self._rolling(15.0), self._logs([19.0, 20.0, 18.0]))
        assert threshold == pytest.approx(3.0)
        assert detected is True

    def test_small_fluctuation_not_detected(self):
        """Normal game-to-game variance within threshold."""
        detected, delta, threshold = _detect_role_change(
            self._rolling(32.0), self._logs([34.0, 31.0, 33.0]))
        # delta = 32.67 - 32.0 = 0.67, threshold = 4.8
        assert detected is False

    def test_fewer_than_3_games_not_detected(self):
        """Requires >= 3 games in logs to trigger."""
        detected, _, _ = _detect_role_change(
            self._rolling(32.0), self._logs([40.0, 40.0]))
        assert detected is False

    def test_demotion_detected_negative_delta(self):
        """Minutes decrease (bench demotion) also triggers."""
        detected, delta, _ = _detect_role_change(
            self._rolling(30.0), self._logs([20.0, 22.0, 21.0]))
        assert detected is True
        assert delta < 0  # role contracted


# ---------------------------------------------------------------------------
# Test 3: ML feature importance extraction
# ---------------------------------------------------------------------------

class TestExtractFeatureImportances:
    """Pin _extract_feature_importances behavior for different estimator types."""

    def test_tree_based_model(self):
        """Estimator with feature_importances_ attribute."""
        class FakeTree:
            feature_importances_ = [0.5, 0.3, 0.2]
        result = _extract_feature_importances(FakeTree(), ["pts", "ast", "reb"])
        assert result is not None
        assert len(result) == 3
        # Sorted descending by importance
        assert result[0]["feature"] == "pts"
        assert result[0]["importance"] == 0.5
        assert result[1]["feature"] == "ast"
        assert result[2]["feature"] == "reb"

    def test_linear_model_normalizes_coefs(self):
        """Estimator with coef_ normalizes to sum-to-1 absolute values."""
        import numpy as np

        class FakeLinear:
            coef_ = np.array([0.4, -0.6, 0.0])
        result = _extract_feature_importances(FakeLinear(), ["a", "b", "c"])
        assert result is not None
        # Total abs = 0.4 + 0.6 + 0.0 = 1.0
        # So normalized: a=0.4, b=0.6, c=0.0
        assert result[0]["feature"] == "b"  # highest absolute
        assert result[0]["importance"] == pytest.approx(0.6, abs=0.01)

    def test_no_importances_returns_none(self):
        """Estimator without importances → None."""
        class FakeBlackBox:
            pass
        result = _extract_feature_importances(FakeBlackBox(), ["a", "b"])
        assert result is None

    def test_calibrated_classifier_reaches_base(self):
        """CalibratedClassifierCV wrapping a tree — extracts from base."""
        class FakeBase:
            feature_importances_ = [0.7, 0.3]

        class FakeCalibratedClassifier:
            estimator = FakeBase()

        class FakeCCV:
            calibrated_classifiers_ = [FakeCalibratedClassifier()]

        # First tries .estimator on the outer CCV — not present
        # Then tries calibrated_classifiers_[0].estimator
        result = _extract_feature_importances(FakeCCV(), ["x", "y"])
        assert result is not None
        assert result[0]["feature"] == "x"
        assert result[0]["importance"] == 0.7
