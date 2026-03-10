"""
tests/test_minutes_model.py — Pin detect_injury_return() and compute_minutes_multiplier() behavior.

Tests cover:
- detect_injury_return: 6 DNPs before first game back → cap_multiplier < 1.0, reasoning includes "injury_return"
- compute_minutes_multiplier: high CV → "high_volatility" in reasoning
- multiplier is clamped between 0.50 and 1.15 (bounds from source)

No network calls. All inputs are synthetic.
"""

import os
import sys
import datetime
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.nba_minutes_model import detect_injury_return, compute_minutes_multiplier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _game_log(date_str: str, minutes: float = 32.0) -> dict:
    """Build a minimal game-log entry."""
    return {"gameDate": date_str, "gameId": "12345", "min": minutes}


def _dnp(date_str: str) -> dict:
    """Build a minimal DNP entry."""
    return {"gameDate": date_str, "gameId": "99999"}


# ---------------------------------------------------------------------------
# Test 1: detect_injury_return — 6 DNPs before first game back
# ---------------------------------------------------------------------------

class TestDetectInjuryReturn:

    def test_six_dnps_first_game_back_applies_cap(self):
        """
        6 DNPs immediately before the most-recent active game.
        _INJURY_CAP_TABLE row: (6, 1, 1, 0.65) → cap = 0.65 when games_since_return=1.

        Active logs (most-recent-first):
          most recent:  2026-02-20 (first game back)
          second:       2026-01-27 (last game before injury)
        DNP dates: 2026-01-29, 2026-01-31, 2026-02-02, 2026-02-05, 2026-02-08, 2026-02-11
        All 6 DNPs fall between 2026-01-27 and 2026-02-20 → consecutive_dnps = 6.
        games_since_return = 1 (only 2026-02-20 is after the last DNP).
        """
        logs = [
            _game_log("2026-02-20", 28.0),
            _game_log("2026-01-27", 34.0),
            _game_log("2026-01-25", 33.0),
        ]
        dnps = [
            _dnp("2026-01-29"),
            _dnp("2026-01-31"),
            _dnp("2026-02-02"),
            _dnp("2026-02-05"),
            _dnp("2026-02-08"),
            _dnp("2026-02-11"),
        ]
        result = detect_injury_return(logs, dnps)
        assert result["is_returning"] is True
        assert result["consecutive_dnps"] == 6
        assert result["games_since_return"] == 1
        assert result["cap_multiplier"] < 1.0
        # Reasoning must include "injury_return"
        assert "injury_return" in result["reasoning"]
        # For 6 DNPs, games_since_return=1 → cap = 0.65
        assert result["cap_multiplier"] == pytest.approx(0.65, abs=0.01)

    def test_no_dnps_no_cap(self):
        """No DNPs → cap_multiplier = 1.0, is_returning = False."""
        logs = [
            _game_log("2026-02-20", 34.0),
            _game_log("2026-02-18", 33.0),
            _game_log("2026-02-16", 35.0),
        ]
        result = detect_injury_return(logs, [])
        assert result["is_returning"] is False
        assert result["cap_multiplier"] == 1.0

    def test_empty_logs_no_cap(self):
        result = detect_injury_return([], [])
        assert result["is_returning"] is False
        assert result["cap_multiplier"] == 1.0

    def test_one_dnp_first_game_back_applies_cap(self):
        """
        1 DNP before first game back.
        _INJURY_CAP_TABLE row: (1, 1, 1, 0.82) → cap = 0.82.
        """
        logs = [
            _game_log("2026-02-20", 30.0),
            _game_log("2026-02-14", 34.0),
        ]
        dnps = [_dnp("2026-02-16")]
        result = detect_injury_return(logs, dnps)
        assert result["is_returning"] is True
        assert result["consecutive_dnps"] == 1
        assert result["cap_multiplier"] < 1.0
        assert "injury_return" in result["reasoning"]

    def test_gap_fallback_triggers_cap(self):
        """
        When API omits DNPs (no excluded_games), a large calendar gap
        between active games triggers the gap-based fallback.
        Gap = 10 days → estimated_dnps = round(10/2.4) = 4.
        games_since_return=2 (one game after gap already in log + upcoming = 2).
        Cap lookup: (3,1,1,0.72) min_dnps=3, min_gsr=1, max_gsr=1 → doesn't match gsr=2.
        Next row: (1,2,2,0.85) → matches. cap=0.85.
        """
        logs = [
            _game_log("2026-02-20", 30.0),
            _game_log("2026-02-10", 34.0),  # 10-day gap
            _game_log("2026-01-25", 35.0),
        ]
        result = detect_injury_return(logs, [])
        # Gap of 10 days >= _LAYOFF_GAP_DAYS (4) → should detect return
        assert result["is_returning"] is True
        assert result["cap_multiplier"] < 1.0


# ---------------------------------------------------------------------------
# Test 2: compute_minutes_multiplier — high volatility
# ---------------------------------------------------------------------------

class TestComputeMinutesMultiplier:

    def _high_vol_rolling(self) -> dict:
        """Rolling dict with CV > 0.28 (high volatility threshold)."""
        # stdev/mean = 10.0/30.0 = 0.333 > 0.28
        return {
            "min_avg5": 30.0,
            "min_avg10": 30.0,
            "min_avg_season": 30.0,
            "min_stdev": 10.0,
        }

    def _stable_rolling(self) -> dict:
        """Rolling dict with CV < 0.10 (low volatility threshold)."""
        # stdev/mean = 2.0/33.0 ≈ 0.061 < 0.10
        return {
            "min_avg5": 33.0,
            "min_avg10": 33.0,
            "min_avg_season": 33.0,
            "min_stdev": 2.0,
        }

    def _stable_logs(self, n: int = 10, minutes: float = 33.0) -> list:
        """Generate n synthetic game logs with consistent minutes."""
        return [_game_log(f"2026-02-{i+1:02d}", minutes) for i in range(n)]

    def test_high_volatility_tag_in_reasoning(self):
        """CV > 0.28 → reasoning includes 'high_volatility'."""
        rolling = self._high_vol_rolling()
        logs = self._stable_logs(10)
        result = compute_minutes_multiplier(rolling, logs)
        assert "high_volatility" in result["minutesReasoning"]

    def test_low_volatility_tag_in_reasoning(self):
        """CV < 0.10 → reasoning includes 'low_volatility'."""
        rolling = self._stable_rolling()
        logs = self._stable_logs(10, 33.0)
        result = compute_minutes_multiplier(rolling, logs)
        assert "low_volatility" in result["minutesReasoning"]

    def test_multiplier_clamped_at_floor(self):
        """Multiplier must always be >= 0.50 (hard floor from source)."""
        # Extremely high volatility + many negative signals
        rolling = {
            "min_avg5": 5.0,
            "min_avg10": 5.0,
            "min_avg_season": 5.0,
            "min_stdev": 10.0,
        }
        logs = self._stable_logs(3, 5.0)
        result = compute_minutes_multiplier(rolling, logs)
        assert result["multiplier"] >= 0.50

    def test_multiplier_clamped_at_ceiling(self):
        """Multiplier must always be <= 1.15 (hard ceiling from source)."""
        # Low volatility stable starter
        rolling = self._stable_rolling()
        logs = self._stable_logs(20, 38.0)
        result = compute_minutes_multiplier(rolling, logs)
        assert result["multiplier"] <= 1.15

    def test_result_keys_present(self):
        """Output dict must have all documented keys."""
        rolling = self._stable_rolling()
        logs = self._stable_logs(10)
        result = compute_minutes_multiplier(rolling, logs)
        for key in ("multiplier", "minutesConfidence", "minutesReasoning",
                    "last5Avg", "last10Avg", "seasonAvg", "volatility"):
            assert key in result, f"Missing key: {key}"

    def test_confidence_bounded(self):
        """minutesConfidence must be in [0.10, 0.95]."""
        rolling = self._high_vol_rolling()
        logs = self._stable_logs(5)
        result = compute_minutes_multiplier(rolling, logs)
        assert 0.10 <= result["minutesConfidence"] <= 0.95

    def test_injury_return_cap_applied_via_excluded_games(self):
        """
        When excluded_games has 6 DNPs and logs show the player just returned,
        the cap_multiplier < 1.0 should reduce the minutes multiplier.
        """
        rolling = self._stable_rolling()
        logs = [
            _game_log("2026-02-20", 28.0),
            _game_log("2026-01-27", 34.0),
            _game_log("2026-01-25", 33.0),
        ]
        dnps = [
            _dnp("2026-01-29"),
            _dnp("2026-01-31"),
            _dnp("2026-02-02"),
            _dnp("2026-02-05"),
            _dnp("2026-02-08"),
            _dnp("2026-02-11"),
        ]
        result = compute_minutes_multiplier(rolling, logs, excluded_games=dnps)
        # Injury return should push multiplier below 1.0
        assert result["multiplier"] < 1.0
        # Reasoning should contain an injury_return tag
        reasoning = result["minutesReasoning"]
        assert any("injury_return" in r for r in reasoning)


# ---------------------------------------------------------------------------
# Test 3: roster_context / mass-absence minutes boost
# ---------------------------------------------------------------------------

class TestMassAbsenceMinutesBoost:
    """Verify roster_context drives mass-absence boost in minutes model."""

    def _rolling(self, avg: float = 32.0) -> dict:
        return {
            "min_avg5": avg,
            "min_avg10": avg,
            "min_avg_season": avg,
            "min_stdev": 2.0,
        }

    def _logs(self, n: int = 10, minutes: float = 32.0) -> list:
        return [_game_log(f"2026-02-{i+1:02d}", minutes) for i in range(n)]

    def test_extreme_tier_starter_gets_boost(self):
        """Starter (avg_s >= 28) in extreme tier gets 1.06x boost."""
        ctx = {"massAbsenceTier": "extreme"}
        # Compute baseline without roster_context
        baseline = compute_minutes_multiplier(
            self._rolling(32.0), self._logs(10, 32.0),
            roster_context=None,
        )
        result = compute_minutes_multiplier(
            self._rolling(32.0), self._logs(10, 32.0),
            roster_context=ctx,
        )
        assert any("mass_absence_extreme_starter" in r for r in result["minutesReasoning"])
        # Boost should be exactly 1.06x of baseline (within floating-point tolerance)
        assert result["multiplier"] == pytest.approx(baseline["multiplier"] * 1.06, abs=0.005)

    def test_extreme_tier_promoted_player_gets_boost(self):
        """Role player (avg_s 20-27) in extreme tier gets 1.04x boost."""
        ctx = {"massAbsenceTier": "extreme"}
        baseline = compute_minutes_multiplier(
            self._rolling(24.0), self._logs(10, 24.0),
            roster_context=None,
        )
        result = compute_minutes_multiplier(
            self._rolling(24.0), self._logs(10, 24.0),
            roster_context=ctx,
        )
        assert any("mass_absence_extreme_promoted" in r for r in result["minutesReasoning"])
        assert result["multiplier"] == pytest.approx(baseline["multiplier"] * 1.04, abs=0.005)

    def test_extreme_tier_bench_no_boost(self):
        """Deep bench player (avg_s < 20) gets no mass-absence boost."""
        ctx = {"massAbsenceTier": "extreme"}
        result = compute_minutes_multiplier(
            self._rolling(15.0), self._logs(10, 15.0),
            roster_context=ctx,
        )
        assert not any("mass_absence" in r for r in result["minutesReasoning"])

    def test_moderate_tier_starter_gets_boost(self):
        """Starter in moderate tier gets 1.03x boost."""
        ctx = {"massAbsenceTier": "moderate"}
        baseline = compute_minutes_multiplier(
            self._rolling(32.0), self._logs(10, 32.0),
            roster_context=None,
        )
        result = compute_minutes_multiplier(
            self._rolling(32.0), self._logs(10, 32.0),
            roster_context=ctx,
        )
        assert any("mass_absence_moderate" in r for r in result["minutesReasoning"])
        assert result["multiplier"] == pytest.approx(baseline["multiplier"] * 1.03, abs=0.005)

    def test_normal_tier_no_boost(self):
        """Normal tier never produces a mass-absence tag."""
        ctx = {"massAbsenceTier": "normal"}
        result = compute_minutes_multiplier(
            self._rolling(32.0), self._logs(10, 32.0),
            roster_context=ctx,
        )
        assert not any("mass_absence" in r for r in result["minutesReasoning"])

    def test_no_roster_context_no_boost(self):
        """Missing roster_context produces no mass-absence tag."""
        result = compute_minutes_multiplier(
            self._rolling(32.0), self._logs(10, 32.0),
            roster_context=None,
        )
        assert not any("mass_absence" in r for r in result["minutesReasoning"])
