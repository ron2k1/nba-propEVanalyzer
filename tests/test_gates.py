"""
tests/test_gates.py — Pin current _qualifies() behavior (FREEZE period).

Rules:
- FREEZE: these tests must mirror current behavior exactly.  Do NOT change
  any source code to make tests pass — if behavior differs from expectation,
  flag the discrepancy in a comment and adjust the assertion to match reality.
- No mocks: all tests call the real _qualifies() with synthetic prop_result dicts.
- Import path: absolute (tests/ is outside core/).

Verified against gates.py as of 2026-03-05 (commit bc016f5).
"""

import pytest
from core.gates import _qualifies, SIGNAL_SPEC, CURRENT_SIGNAL_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_prop(
    *,
    probOver: float = 0.05,
    over_edge: float = 0.10,
    under_edge: float = 0.00,
    probUnder: float | None = None,
    clv_line: float | None = None,
    clv_odds: float | None = None,
    minutes_reasoning: list | None = None,
    reference_book: dict | None = None,
    recent_high_variance: bool = False,
    n_books_offering: int | None = None,
    games_played: int | None = None,
    season_minutes: float | None = None,
) -> dict:
    """
    Build a minimal prop_result dict that _qualifies() can consume.

    Defaults produce a bin-0, pts-eligible, passing signal when stat='pts'
    and used_real_line is omitted (None) or True:
      probOver=0.05, over_edge=0.10, under_edge=0.00, probUnder=0.95

    All fields are placed exactly where _qualifies() looks for them.
    """
    computed_prob_under = probUnder if probUnder is not None else (1.0 - probOver)
    result: dict = {
        "ev": {
            "over":  {"edge": over_edge},
            "under": {"edge": under_edge},
            "probOver":  probOver,
            "probUnder": computed_prob_under,
        },
    }
    if clv_line is not None:
        result["clvLine"] = clv_line
    if clv_odds is not None:
        result["clvOddsPct"] = clv_odds
    if minutes_reasoning is not None:
        result["minutesProjection"] = {"minutesReasoning": minutes_reasoning}
    if reference_book is not None:
        result["referenceBook"] = reference_book
    if n_books_offering is not None:
        result["nBooksOffering"] = n_books_offering
    if recent_high_variance:
        result["projection"] = {"recentHighVariance": True}
    if games_played is not None:
        result["gamesPlayed"] = games_played
    if season_minutes is not None:
        result.setdefault("minutesProjection", {})["seasonMinutes"] = season_minutes
    return result


# ---------------------------------------------------------------------------
# Sanity: confirm SIGNAL_SPEC shape hasn't drifted
# ---------------------------------------------------------------------------

class TestSignalSpecInvariants:
    """Pin SIGNAL_SPEC constants; fail loudly if a freeze-period edit occurs."""

    def test_current_version_is_v1(self):
        assert CURRENT_SIGNAL_VERSION == "v1"

    def test_eligible_stats(self):
        spec = SIGNAL_SPEC["v1"]
        assert spec["eligible_stats"] == {"pts", "reb", "ast"}

    def test_blocked_bins_exact(self):
        spec = SIGNAL_SPEC["v1"]
        assert spec["blocked_prob_bins"] == {1, 2, 3, 4, 5, 6, 7, 8}

    def test_min_edge_global(self):
        assert SIGNAL_SPEC["v1"]["min_edge"] == 0.08

    def test_min_edge_ast_elevated(self):
        assert SIGNAL_SPEC["v1"]["min_edge_by_stat"]["ast"] == 0.09

    def test_min_confidence(self):
        assert SIGNAL_SPEC["v1"]["min_confidence"] == 0.60

    def test_real_line_required_stats(self):
        assert SIGNAL_SPEC["v1"]["real_line_required_stats"] == {"reb"}

    def test_min_games_played(self):
        assert SIGNAL_SPEC["v1"]["min_games_played"] == 10

    def test_min_season_avg_minutes(self):
        assert SIGNAL_SPEC["v1"]["min_season_avg_minutes"] == 10.0

    def test_min_books_offering(self):
        assert SIGNAL_SPEC["v1"]["min_books_offering"] == 2

    def test_pinnacle_per_stat_thresholds(self):
        pbs = SIGNAL_SPEC["v1"]["pinnacle_min_no_vig_by_stat"]
        assert pbs["pts"] == 0.62
        assert pbs["ast"] == 0.67
        assert pbs["reb"] == 0.62

    def test_pinnacle_global_thresholds_both_bins(self):
        pt = SIGNAL_SPEC["v1"]["pinnacle_thresholds"]
        # Bin 0 and bin 9 both set to 0.75 (conservative until 30+ bin-9 bets)
        assert pt[0] == 0.75
        assert pt[9] == 0.75


# ---------------------------------------------------------------------------
# Gate 1 — stat eligibility
# ---------------------------------------------------------------------------

class TestStatEligibility:

    def test_ineligible_stat_tov(self):
        prop = _make_prop()
        ok, reason = _qualifies(prop, stat="tov")
        assert ok is False
        assert reason.startswith("stat_not_eligible")

    def test_ineligible_stat_stl(self):
        prop = _make_prop()
        ok, reason = _qualifies(prop, stat="stl")
        assert ok is False
        assert reason.startswith("stat_not_eligible")

    def test_ineligible_stat_blk(self):
        prop = _make_prop()
        ok, reason = _qualifies(prop, stat="blk")
        assert ok is False
        assert reason.startswith("stat_not_eligible")

    def test_ineligible_stat_fg3m(self):
        prop = _make_prop()
        ok, reason = _qualifies(prop, stat="fg3m")
        assert ok is False
        assert reason.startswith("stat_not_eligible")

    def test_ineligible_stat_pra(self):
        # pra removed 2026-03-01: -3.81% ROI
        prop = _make_prop()
        ok, reason = _qualifies(prop, stat="pra")
        assert ok is False
        assert reason.startswith("stat_not_eligible")

    def test_pts_is_eligible_gate_reached(self):
        # pts should pass the stat gate; confirm failure reason is NOT stat_not_eligible
        prop = _make_prop(over_edge=0.00, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts")
        assert not reason.startswith("stat_not_eligible")

    def test_ast_is_eligible_gate_reached(self):
        prop = _make_prop(over_edge=0.00, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="ast")
        assert not reason.startswith("stat_not_eligible")

    def test_reb_is_eligible_gate_reached(self):
        # reb requires used_real_line=True to survive to the next gate
        prop = _make_prop(over_edge=0.00, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="reb", used_real_line=True)
        assert not reason.startswith("stat_not_eligible")

    def test_stat_case_insensitive(self):
        # _qualifies coerces to lower: "PTS" should pass stat gate
        prop = _make_prop(over_edge=0.00)
        ok, reason = _qualifies(prop, stat="PTS")
        assert not reason.startswith("stat_not_eligible")


# ---------------------------------------------------------------------------
# Gate 2 — real_line_required (reb only)
# ---------------------------------------------------------------------------

class TestRealLineRequired:

    def test_reb_without_real_line_fails(self):
        # used_real_line=None → treated as False for reb
        prop = _make_prop()
        ok, reason = _qualifies(prop, stat="reb", used_real_line=None)
        assert ok is False
        assert "real_line_required" in reason

    def test_reb_used_real_line_false_fails(self):
        prop = _make_prop()
        ok, reason = _qualifies(prop, stat="reb", used_real_line=False)
        assert ok is False
        assert "real_line_required" in reason

    def test_reb_used_real_line_true_passes_gate(self):
        # Edge set to zero so it fails the NEXT gate — confirms real_line gate passed
        prop = _make_prop(over_edge=0.00, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="reb", used_real_line=True)
        assert not reason.startswith("real_line_required")

    def test_pts_ignores_real_line_flag(self):
        # pts is NOT in real_line_required_stats — used_real_line value is irrelevant
        prop = _make_prop(over_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts", used_real_line=False)
        assert not reason.startswith("real_line_required")

    def test_ast_ignores_real_line_flag(self):
        prop = _make_prop(over_edge=0.00)
        ok, reason = _qualifies(prop, stat="ast", used_real_line=False)
        assert not reason.startswith("real_line_required")


# ---------------------------------------------------------------------------
# Gate 2b — player quality (games played + season minutes)
# ---------------------------------------------------------------------------

class TestPlayerQualityGates:
    """
    Player quality gates fire only when gamesPlayed is present (live pipeline).
    Absent gamesPlayed (backtest) → gate skipped entirely.
    """

    def test_games_played_absent_skips_gate(self):
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert "insufficient_games" not in reason

    def test_games_played_below_threshold_fails(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            games_played=3,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "insufficient_games:3" in reason

    def test_games_played_at_threshold_passes(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            games_played=10,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "insufficient_games" not in reason

    def test_games_played_above_threshold_passes(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            games_played=50,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "insufficient_games" not in reason

    def test_games_played_zero_skips_gate(self):
        # gamesPlayed=0: condition is `0 < _gp` which is False → gate skipped
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            games_played=0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "insufficient_games" not in reason

    def test_season_minutes_below_threshold_fails(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            games_played=20, season_minutes=5.5,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "low_minutes_player:5.5" in reason

    def test_season_minutes_at_threshold_passes(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            games_played=20, season_minutes=10.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "low_minutes_player" not in reason

    def test_season_minutes_above_threshold_passes(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            games_played=20, season_minutes=30.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "low_minutes_player" not in reason

    def test_season_minutes_zero_skips_gate(self):
        # seasonMinutes=0: condition `_sam > 0` is False → gate skipped
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            games_played=20, season_minutes=0.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "low_minutes_player" not in reason

    def test_season_minutes_skipped_when_games_absent(self):
        # gamesPlayed absent → entire quality block skipped, even with low minutes
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            season_minutes=2.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "low_minutes_player" not in reason
        assert "insufficient_games" not in reason


# ---------------------------------------------------------------------------
# Gate 3 — edge_too_low
# ---------------------------------------------------------------------------

class TestEdgeTooLow:

    # --- pts: global min_edge = 0.08 ---

    def test_pts_edge_exactly_zero_fails(self):
        prop = _make_prop(over_edge=0.00, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "edge_too_low" in reason

    def test_pts_edge_below_threshold_fails(self):
        # 0.0799 < 0.08
        prop = _make_prop(over_edge=0.0799, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "edge_too_low" in reason

    def test_pts_edge_exactly_at_threshold_passes_gate(self):
        # 0.08 is NOT < 0.08 → gate passes; subsequent gates may still block
        prop = _make_prop(over_edge=0.08, probOver=0.05, probUnder=0.95)
        ok, reason = _qualifies(prop, stat="pts")
        assert "edge_too_low" not in reason

    def test_pts_edge_above_threshold_passes_gate(self):
        prop = _make_prop(over_edge=0.12)
        ok, reason = _qualifies(prop, stat="pts")
        assert "edge_too_low" not in reason

    def test_pts_under_edge_sufficient(self):
        # max(eo, eu) uses whichever side is larger
        prop = _make_prop(over_edge=0.00, under_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert "edge_too_low" not in reason

    # --- ast: min_edge_by_stat = 0.09 ---

    def test_ast_edge_at_pts_threshold_fails(self):
        # 0.08 < 0.09 for ast
        prop = _make_prop(over_edge=0.08, probOver=0.05, probUnder=0.95)
        ok, reason = _qualifies(prop, stat="ast")
        assert ok is False
        assert "edge_too_low" in reason

    def test_ast_edge_exactly_at_ast_threshold_passes_gate(self):
        # 0.09 is NOT < 0.09 → gate passes
        prop = _make_prop(over_edge=0.09, probOver=0.05, probUnder=0.95)
        ok, reason = _qualifies(prop, stat="ast")
        assert "edge_too_low" not in reason

    def test_ast_edge_below_ast_threshold_fails(self):
        prop = _make_prop(over_edge=0.0899, probOver=0.05, probUnder=0.95)
        ok, reason = _qualifies(prop, stat="ast")
        assert ok is False
        assert "edge_too_low" in reason

    # --- reb: min_edge_by_stat = 0.08 (same as global) ---

    def test_reb_edge_at_threshold_passes_gate(self):
        prop = _make_prop(over_edge=0.08, probOver=0.05, probUnder=0.95)
        ok, reason = _qualifies(prop, stat="reb", used_real_line=True)
        assert "edge_too_low" not in reason


# ---------------------------------------------------------------------------
# Gate 4 — confidence_too_low
# ---------------------------------------------------------------------------

class TestConfidenceTooLow:

    def test_conf_below_threshold_fails(self):
        # conf = max(probOver, probUnder) = max(0.50, 0.50) = 0.50 < 0.60
        prop = _make_prop(
            probOver=0.50,
            probUnder=0.50,
            over_edge=0.10,
            under_edge=0.10,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "confidence_too_low" in reason

    def test_conf_exactly_at_threshold_passes_gate(self):
        # conf = max(0.60, 0.40) = 0.60 → NOT < 0.60 → passes
        # probOver=0.40 → bin 4 (blocked) → next gate will block, but confidence passed
        prop = _make_prop(probOver=0.40, probUnder=0.60, over_edge=0.10, under_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert "confidence_too_low" not in reason

    def test_conf_above_threshold_passes_gate(self):
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts")
        assert "confidence_too_low" not in reason

    def test_conf_derived_from_prob_under(self):
        # probUnder drives conf when probOver is low
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.00, under_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert "confidence_too_low" not in reason

    def test_conf_just_below_threshold_fails(self):
        # 0.599 < 0.60
        prop = _make_prop(probOver=0.401, probUnder=0.599, over_edge=0.10, under_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "confidence_too_low" in reason


# ---------------------------------------------------------------------------
# Gate 5 — blocked_prob_bin
# ---------------------------------------------------------------------------

class TestBlockedProbBin:

    @pytest.mark.parametrize("prob_over,expected_bin", [
        (0.10, 1),
        (0.15, 1),
        (0.19, 1),
        (0.20, 2),
        (0.30, 3),
        (0.40, 4),
        (0.50, 5),
        (0.60, 6),
        (0.70, 7),
        (0.75, 7),
        (0.80, 8),
        (0.85, 8),
    ])
    def test_blocked_bins_fail(self, prob_over: float, expected_bin: int):
        """Bins 1–8 are all blocked; test representative probOver values."""
        # Use probUnder to drive confidence above 0.60 where needed
        prob_under = 1.0 - prob_over
        confidence = max(prob_over, prob_under)
        # Only test bins where confidence would be >= 0.60
        if confidence < 0.60:
            pytest.skip(f"probOver={prob_over} yields conf={confidence} < 0.60; blocked by earlier gate")
        prop = _make_prop(
            probOver=prob_over,
            probUnder=prob_under,
            over_edge=0.10,
            under_edge=0.10,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "blocked_prob_bin" in reason
        assert str(expected_bin) in reason

    def test_bin_0_passes(self):
        # probOver=0.05 → int(0.5)=0 → bin 0 (not in blocked set)
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts")
        assert "blocked_prob_bin" not in reason

    def test_bin_0_boundary_passes(self):
        # probOver=0.09 → int(0.9)=0 → bin 0
        prop = _make_prop(probOver=0.09, probUnder=0.91, over_edge=0.10, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts")
        assert "blocked_prob_bin" not in reason

    def test_bin_1_boundary_at_0_10_fails(self):
        # probOver=0.10 → int(1.0)=1 → bin 1 (blocked)
        prop = _make_prop(probOver=0.10, probUnder=0.90, over_edge=0.10, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "blocked_prob_bin:1" in reason

    def test_bin_9_passes(self):
        # probOver=0.95 → int(9.5)=9 → bin 9 (not in blocked set)
        prop = _make_prop(probOver=0.95, probUnder=0.05, over_edge=0.10, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts")
        assert "blocked_prob_bin" not in reason

    def test_bin_9_boundary_at_0_90_fails(self):
        # probOver=0.90 → int(9.0)=9 → bin 9 (NOT blocked)
        # NOTE: 0.90 maps to bin 9 (int(0.90*10)=int(9.0)=9), which passes.
        # This is a subtle boundary: bin 8 ends at probOver<0.90, bin 9 starts at 0.90.
        prop = _make_prop(probOver=0.90, probUnder=0.10, over_edge=0.10, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts")
        assert "blocked_prob_bin" not in reason

    def test_bin_8_at_0_89_fails(self):
        # probOver=0.89 → int(8.9)=8 → bin 8 (blocked)
        prop = _make_prop(probOver=0.89, probUnder=0.11, over_edge=0.10, under_edge=0.00)
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "blocked_prob_bin:8" in reason


# ---------------------------------------------------------------------------
# Gate 6 — CLV gate
# ---------------------------------------------------------------------------

class TestClvGate:

    def test_both_clv_absent_skips_gate(self):
        # No clvLine/clvOddsPct keys → gate is skipped entirely
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert "clv_gate_failed" not in reason

    def test_only_clv_line_present_skips_gate(self):
        # Only one of the two CLV fields → gate is skipped (both must be present)
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10, clv_line=1.5)
        ok, reason = _qualifies(prop, stat="pts")
        assert "clv_gate_failed" not in reason

    def test_only_clv_odds_present_skips_gate(self):
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10, clv_odds=2.0)
        ok, reason = _qualifies(prop, stat="pts")
        assert "clv_gate_failed" not in reason

    def test_both_clv_positive_passes(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            clv_line=0.5, clv_odds=1.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "clv_gate_failed" not in reason

    def test_clv_line_zero_passes(self):
        # CLV=0 is neutral — should pass (not strictly negative)
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            clv_line=0.0, clv_odds=1.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "clv_gate_failed" not in reason

    def test_clv_line_negative_fails(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            clv_line=-0.5, clv_odds=1.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "clv_gate_failed" in reason

    def test_clv_odds_zero_passes(self):
        # CLV odds=0 is neutral — should pass
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            clv_line=1.0, clv_odds=0.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "clv_gate_failed" not in reason

    def test_clv_odds_negative_fails(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            clv_line=0.5, clv_odds=-1.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "clv_gate_failed" in reason

    def test_both_clv_zero_passes(self):
        # Both zero (completely neutral) should pass
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            clv_line=0.0, clv_odds=0.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "clv_gate_failed" not in reason

    def test_clv_mixed_sign_positive_line_negative_odds_fails(self):
        # Line moved favorably but odds moved against — still block
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            clv_line=0.5, clv_odds=-2.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "clv_gate_failed" in reason

    def test_clv_mixed_sign_negative_line_positive_odds_fails(self):
        # Line moved against but odds moved favorably — still block
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            clv_line=-0.5, clv_odds=2.0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "clv_gate_failed" in reason


# ---------------------------------------------------------------------------
# Gate 7 — injury return block
# ---------------------------------------------------------------------------

class TestInjuryReturnBlock:

    def test_injury_return_g1_at_72pct_blocked(self):
        # pct=72 <= 72 → blocked
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            minutes_reasoning=["injury_return_g1:72pct"],
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "injury_return_g1_blocked" in reason

    def test_injury_return_g1_below_72pct_blocked(self):
        # pct=60 <= 72 → blocked
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            minutes_reasoning=["injury_return_g1:60pct"],
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "injury_return_g1_blocked" in reason

    def test_injury_return_g1_above_72pct_passes(self):
        # pct=73 > 72 → NOT blocked
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            minutes_reasoning=["injury_return_g1:73pct"],
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "injury_return_g1_blocked" not in reason

    def test_injury_return_gap_tag_at_72pct_blocked(self):
        # Calendar-gap tag: "injury_return_gap_10d_g1_cap_72pct"
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            minutes_reasoning=["injury_return_gap_10d_g1_cap_72pct"],
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "injury_return_g1_blocked" in reason

    def test_injury_return_gap_tag_above_72pct_passes(self):
        # pct=80 > 72 → NOT blocked
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            minutes_reasoning=["injury_return_gap_10d_g1_cap_80pct"],
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "injury_return_g1_blocked" not in reason

    def test_no_injury_tag_no_block(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            minutes_reasoning=["normal_role", "starter_confirmed"],
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "injury_return_g1_blocked" not in reason

    def test_empty_minutes_reasoning_no_block(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            minutes_reasoning=[],
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "injury_return_g1_blocked" not in reason

    def test_minutes_reasoning_absent_no_block(self):
        # minutesProjection key entirely absent
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert "injury_return_g1_blocked" not in reason


# ---------------------------------------------------------------------------
# Gate 8 — Pinnacle confirmation gate
# ---------------------------------------------------------------------------

class TestPinnacleGate:
    """
    Pinnacle gate fires only when referenceBook is present.
    Absent referenceBook → pass-through (backtest compat).

    Per-stat thresholds (pinnacle_min_no_vig_by_stat) override global
    pinnacle_thresholds when set.  For bin-0 UNDER signals:
      pts: 0.62  ast: 0.67  reb: 0.62
    Global fallback (bin 0 / bin 9): 0.75

    rec_side = "over" if over_edge >= under_edge else "under"
    """

    def test_no_reference_book_skips_gate(self):
        # referenceBook absent → Pinnacle gate skipped entirely
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert "pinnacle" not in reason.lower()

    def test_no_reference_book_empty_dict_skips_gate(self):
        # referenceBook={} is falsy → gate skipped
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10,
                          reference_book={})
        ok, reason = _qualifies(prop, stat="pts")
        assert "no_pinnacle_no_vig" not in reason
        assert "pinnacle_" not in reason

    def test_pts_under_side_no_vig_below_stat_threshold_fails(self):
        # rec_side=under (under_edge > over_edge), pts threshold = 0.62
        # noVigUnder=0.61 < 0.62 → fails
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.10,
            reference_book={"noVigUnder": 0.61},
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "pinnacle_under_too_low" in reason

    def test_pts_under_side_no_vig_at_threshold_passes(self):
        # noVigUnder=0.62 == 0.62 → NOT < 0.62 → passes
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.10,
            reference_book={"noVigUnder": 0.62},
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "pinnacle_under_too_low" not in reason

    def test_pts_under_side_no_vig_above_threshold_passes(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.10,
            reference_book={"noVigUnder": 0.80},
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "pinnacle_under_too_low" not in reason

    def test_ast_under_threshold_is_higher(self):
        # ast threshold = 0.67 (higher bar than pts=0.62)
        # noVigUnder=0.65 passes for pts but fails for ast
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.10,
            reference_book={"noVigUnder": 0.65},
        )
        ok_pts, _ = _qualifies(prop, stat="pts")
        ok_ast, reason_ast = _qualifies(prop, stat="ast")
        # pts passes Pinnacle gate at 0.65 (>= 0.62)
        assert "pinnacle_under_too_low" not in _
        # ast fails Pinnacle gate at 0.65 (< 0.67)
        assert ok_ast is False
        assert "pinnacle_under_too_low" in reason_ast

    def test_no_vig_key_absent_on_recommended_side_fails(self):
        # referenceBook present but noVigUnder is missing → "no_pinnacle_no_vig_under"
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.10,
            reference_book={"noVigOver": 0.80},  # wrong side key
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "no_pinnacle_no_vig_under" in reason

    def test_over_side_recommended_checks_no_vig_over(self):
        # over_edge > under_edge → rec_side=over → checks noVigOver, pts threshold 0.62
        prop = _make_prop(
            probOver=0.95, probUnder=0.05,
            over_edge=0.10, under_edge=0.00,
            reference_book={"noVigOver": 0.61},
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "pinnacle_over_too_low" in reason

    def test_over_side_no_vig_at_threshold_passes(self):
        prop = _make_prop(
            probOver=0.95, probUnder=0.05,
            over_edge=0.10, under_edge=0.00,
            reference_book={"noVigOver": 0.62},
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "pinnacle_over_too_low" not in reason

    def test_tie_over_edge_equals_under_edge_rec_side_is_over(self):
        # When eo == eu, the code uses "over" (>= condition)
        prop = _make_prop(
            probOver=0.95, probUnder=0.05,
            over_edge=0.10, under_edge=0.10,
            reference_book={"noVigOver": 0.61},  # insufficient for pts
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "pinnacle_over_too_low" in reason


# ---------------------------------------------------------------------------
# Gate 9 — high variance block
# ---------------------------------------------------------------------------

class TestHighVarianceBlock:

    def test_high_variance_true_fails(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            recent_high_variance=True,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert reason == "recent_high_variance"

    def test_high_variance_false_passes_gate(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            recent_high_variance=False,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "recent_high_variance" not in reason

    def test_high_variance_absent_passes_gate(self):
        # projection key absent entirely
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert "recent_high_variance" not in reason

    def test_high_variance_non_true_value_passes_gate(self):
        # recentHighVariance=False explicitly — gate uses `is True` check, so only
        # the boolean True triggers the block
        prop = {
            "ev": {
                "over":  {"edge": 0.10},
                "under": {"edge": 0.00},
                "probOver":  0.05,
                "probUnder": 0.95,
            },
            "projection": {"recentHighVariance": False},
        }
        ok, reason = _qualifies(prop, stat="pts")
        assert "recent_high_variance" not in reason


# ---------------------------------------------------------------------------
# Gate 10 — market depth (nBooksOffering)
# ---------------------------------------------------------------------------

class TestMarketDepth:

    def test_n_books_absent_skips_gate(self):
        # nBooksOffering key not present → gate skipped (backtest compat)
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert "only_one_book" not in reason

    def test_n_books_1_fails(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            n_books_offering=1,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert "only_one_book" in reason

    def test_n_books_2_passes(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            n_books_offering=2,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "only_one_book" not in reason

    def test_n_books_3_passes(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            n_books_offering=3,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "only_one_book" not in reason

    def test_n_books_0_skips_gate(self):
        # BEHAVIOR NOTE: the gate condition is `_n_books > 0 and _n_books < _min_books`.
        # nBooksOffering=0 → 0 > 0 is False → gate is NOT triggered.
        # This means "no books offering" is treated the same as "absent" (passes through).
        # If this behavior seems surprising, it exists because 0 typically means
        # "data not collected" rather than "literally 0 books offering the line".
        prop = _make_prop(
            probOver=0.05, probUnder=0.95, over_edge=0.10,
            n_books_offering=0,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert "only_one_book" not in reason


# ---------------------------------------------------------------------------
# Full qualifying signal — all gates pass
# ---------------------------------------------------------------------------

class TestFullQualifyingSignal:
    """
    A signal where every condition is met should return (True, "").
    These tests use no referenceBook (Pinnacle gate skipped) and no CLV
    fields (CLV gate skipped) for simplicity — both are legitimate omissions
    in backtest / paper-trade contexts.
    """

    def test_pts_bin0_no_extras(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.12,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is True
        assert reason == ""

    def test_ast_bin0_higher_edge(self):
        # ast requires edge >= 0.09
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.09,
        )
        ok, reason = _qualifies(prop, stat="ast")
        assert ok is True
        assert reason == ""

    def test_reb_bin0_with_real_line(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.10,
        )
        ok, reason = _qualifies(prop, stat="reb", used_real_line=True)
        assert ok is True
        assert reason == ""

    def test_pts_bin9_over_signal(self):
        # bin 9: probOver=0.95 → int(9.5)=9 → passes
        prop = _make_prop(
            probOver=0.95, probUnder=0.05,
            over_edge=0.12, under_edge=0.00,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is True
        assert reason == ""

    def test_full_signal_with_positive_clv(self):
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.12,
            clv_line=0.5, clv_odds=2.1,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is True
        assert reason == ""

    def test_full_signal_with_pinnacle_above_threshold(self):
        # pts threshold = 0.62; noVigUnder=0.75 passes
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.12,
            reference_book={"noVigUnder": 0.75},
            n_books_offering=2,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is True
        assert reason == ""

    def test_full_signal_with_all_optional_gates_present(self):
        """Most complete possible qualifying signal."""
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.12,
            clv_line=1.0, clv_odds=3.0,
            minutes_reasoning=["starter_confirmed", "injury_return_g1:80pct"],
            reference_book={"noVigUnder": 0.70},
            recent_high_variance=False,
            n_books_offering=3,
        )
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is True
        assert reason == ""

    def test_none_prop_result_returns_edge_too_low(self):
        # _qualifies guards against None prop_result via `(prop_result or {})`
        # With empty ev dict, edge=0.0 and conf=0.0 → edge_too_low fires first
        ok, reason = _qualifies(None, stat="pts")
        assert ok is False
        assert "edge_too_low" in reason


# ---------------------------------------------------------------------------
# Bin boundary exhaustive: confirm exact int(probOver * 10) mapping
# ---------------------------------------------------------------------------

class TestBinBoundaryMapping:
    """
    Documents the exact bin boundaries produced by:
        bin_idx = max(0, min(9, int(prob_over * 10)))
    These drive both the blocked-bin gate and the global Pinnacle threshold lookup.
    """

    @pytest.mark.parametrize("prob_over,expected_bin,blocked", [
        (0.00, 0, False),
        (0.05, 0, False),
        (0.099, 0, False),
        (0.10, 1, True),
        (0.199, 1, True),
        (0.20, 2, True),
        (0.299, 2, True),
        (0.30, 3, True),
        (0.399, 3, True),
        (0.40, 4, True),
        (0.499, 4, True),
        (0.50, 5, True),
        (0.599, 5, True),
        (0.60, 6, True),
        (0.699, 6, True),
        (0.70, 7, True),
        (0.799, 7, True),
        (0.80, 8, True),
        (0.899, 8, True),
        (0.90, 9, False),
        (0.95, 9, False),
        (1.00, 9, False),   # max(0, min(9, int(10.0))) = 9
    ])
    def test_bin_mapping(self, prob_over, expected_bin, blocked):
        computed_bin = max(0, min(9, int(prob_over * 10)))
        assert computed_bin == expected_bin, (
            f"probOver={prob_over} → bin={computed_bin}, expected {expected_bin}"
        )
        is_blocked = computed_bin in SIGNAL_SPEC["v1"]["blocked_prob_bins"]
        assert is_blocked == blocked, (
            f"bin {computed_bin} blocked={is_blocked}, expected {blocked}"
        )


# ---------------------------------------------------------------------------
# Gate 11 — star replacement block
# ---------------------------------------------------------------------------

class TestStarReplacementBlock:
    """
    When a backup player is replacing a star (cap hit + absent USG >= 2x target),
    the signal should be blocked with 'star_replacement_block'.
    """

    def test_star_replacement_flag_true_blocks(self):
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10)
        prop["starReplacementFlag"] = True
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert reason == "star_replacement_block"

    def test_star_replacement_flag_false_passes(self):
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10)
        prop["starReplacementFlag"] = False
        ok, reason = _qualifies(prop, stat="pts")
        assert "star_replacement_block" not in reason

    def test_star_replacement_flag_absent_passes(self):
        prop = _make_prop(probOver=0.05, probUnder=0.95, over_edge=0.10)
        ok, reason = _qualifies(prop, stat="pts")
        assert "star_replacement_block" not in reason

    def test_star_replacement_blocks_before_other_gates(self):
        """Star replacement should block even if all other gates would pass."""
        prop = _make_prop(
            probOver=0.05, probUnder=0.95,
            over_edge=0.00, under_edge=0.12,
        )
        prop["starReplacementFlag"] = True
        ok, reason = _qualifies(prop, stat="pts")
        assert ok is False
        assert reason == "star_replacement_block"
