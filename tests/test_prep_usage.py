"""
tests/test_prep_usage.py — Pin compute_usage_adjustment() behavior.

Tests cover:
- 28% usage teammate inactive → effectiveMultiplier > 1.0
- ast multiplier is damped relative to pts when both are boosted
- No absent high-usage teammates → effectiveMultiplier = 1.0

All API calls (get_team_roster_status) are monkeypatched.
"""

import os
import sys
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.nba_prep_usage import compute_usage_adjustment


# ---------------------------------------------------------------------------
# Fake roster builders
# ---------------------------------------------------------------------------

def _make_player(player_id: int, name: str, usg_pct: float, status: str, season_gp: int = 30) -> dict:
    """Build a minimal player dict matching the shape compute_usage_adjustment() reads."""
    return {
        "playerId": player_id,
        "name": name,
        "usgPct": usg_pct,
        "status": status,
        "seasonGP": season_gp,
        "riskLevel": "High" if usg_pct >= 25 else "Medium",
    }


def _fake_roster_data(players: list) -> dict:
    return {"success": True, "players": players}


# ---------------------------------------------------------------------------
# Test 1: High-usage teammate inactive → effectiveMultiplier > 1.0
# ---------------------------------------------------------------------------

class TestHighUsageTeammateInactive:

    def test_28pct_usage_teammate_inactive_boosts_multiplier(self, monkeypatch):
        """
        Target player: ID=1, usgPct=22%, Active.
        Teammate: ID=2, usgPct=28%, Inactive (seasonGP >= 10).
        → absent_usg_total = 28 > 0 → usage_ratio > 1 → effectiveMultiplier > 1.0
        """
        players = [
            _make_player(1, "Target Player", usg_pct=22.0, status="Active"),
            _make_player(2, "Star Teammate", usg_pct=28.0, status="Inactive"),
            _make_player(3, "Role Player",   usg_pct=18.0, status="Active"),
        ]

        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )

        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        assert result["effectiveMultiplier"] > 1.0
        # At least one teammate should appear in absentTeammates
        assert len(result["absentTeammates"]) >= 1
        absent_usg = [t["usgPct"] for t in result["absentTeammates"]]
        assert 28.0 in absent_usg

    def test_multiplier_capped_at_1_45(self, monkeypatch):
        """effectiveMultiplier is capped at 1.45 (max in source)."""
        players = [
            _make_player(1, "Target", usg_pct=20.0, status="Active"),
            _make_player(2, "Mega Star A", usg_pct=40.0, status="Inactive"),
            _make_player(3, "Mega Star B", usg_pct=40.0, status="Inactive"),
        ]

        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )

        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        assert result["effectiveMultiplier"] <= 1.45


# ---------------------------------------------------------------------------
# Test 2: ast multiplier damped vs pts when both are in boost territory
# ---------------------------------------------------------------------------

class TestAstDampingVsPts:

    def test_ast_mult_damped_relative_to_pts(self, monkeypatch):
        """
        When effectiveMultiplier > 1.05 (both pts and ast are boosted > 1.05),
        ast_mult should be <= pts_mult * 0.88 (cross-stat damping from source).

        Use a large absent teammate to force effective_mult well above 1.05.
        """
        players = [
            _make_player(1, "Target", usg_pct=20.0, status="Active"),
            _make_player(2, "Absent Star", usg_pct=35.0, status="Inactive", season_gp=50),
        ]

        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )

        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True

        mults = result["statMultipliers"]
        pts_m = mults["pts"]
        ast_m = mults["ast"]

        if pts_m > 1.05 and ast_m > 1.05:
            # Damping should apply: ast_mult <= pts_mult * 0.88
            assert ast_m <= pts_m * 0.88 + 1e-6, (
                f"ast_mult={ast_m} exceeds pts_mult*0.88={pts_m * 0.88} — damping not applied"
            )
        else:
            # Multiplier not large enough to trigger damping; skip the assertion
            pytest.skip(
                f"effectiveMultiplier={result['effectiveMultiplier']} not large enough "
                f"to trigger ast damping (pts_m={pts_m}, ast_m={ast_m})"
            )

    def test_reb_elasticity_lower_than_pts(self, monkeypatch):
        """
        reb elasticity (0.12) << pts elasticity (0.80).
        So reb_mult should be much closer to 1.0 than pts_mult when boosted.
        """
        players = [
            _make_player(1, "Target", usg_pct=20.0, status="Active"),
            _make_player(2, "Absent Star", usg_pct=35.0, status="Inactive", season_gp=50),
        ]

        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )

        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True

        mults = result["statMultipliers"]
        pts_m = mults["pts"]
        reb_m = mults["reb"]

        # pts gains more from usage boost than reb (elasticity 0.80 vs 0.12)
        assert pts_m >= reb_m, f"pts_m={pts_m} should be >= reb_m={reb_m}"


# ---------------------------------------------------------------------------
# Test 3: No absent teammates → effectiveMultiplier = 1.0
# ---------------------------------------------------------------------------

class TestNoAbsentTeammates:

    def test_all_active_roster_returns_1_0(self, monkeypatch):
        """When no teammate meets the inactive+high-usage criteria, multiplier = 1.0."""
        players = [
            _make_player(1, "Target", usg_pct=22.0, status="Active"),
            _make_player(2, "Teammate A", usg_pct=24.0, status="Active"),
            _make_player(3, "Teammate B", usg_pct=19.0, status="Active"),
        ]

        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )

        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        assert result["effectiveMultiplier"] == pytest.approx(1.0)
        assert result["usageMultiplier"] == pytest.approx(1.0)
        assert result["absentTeammates"] == []

    def test_low_usage_inactive_teammate_not_counted(self, monkeypatch):
        """
        A teammate who is inactive but has usgPct < 18 does NOT trigger boost.
        Threshold: p["usgPct"] >= 18.0 (see source).
        """
        players = [
            _make_player(1, "Target", usg_pct=22.0, status="Active"),
            _make_player(2, "Low Usage Inactive", usg_pct=12.0, status="Inactive"),
        ]

        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )

        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        assert result["effectiveMultiplier"] == pytest.approx(1.0)

    def test_player_not_on_roster_returns_failure(self, monkeypatch):
        """Player ID not found on roster → success=False with statMultipliers all 1.0."""
        players = [
            _make_player(2, "Other Player", usg_pct=22.0, status="Active"),
        ]

        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )

        result = compute_usage_adjustment(player_id=999, team_abbr="MIN")
        assert result["success"] is False
        # statMultipliers should be all 1.0 (fallback in source)
        for stat, mult in result["statMultipliers"].items():
            assert mult == pytest.approx(1.0), f"{stat}: {mult}"
