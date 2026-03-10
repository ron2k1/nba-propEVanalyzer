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

def _make_player(player_id: int, name: str, usg_pct: float, status: str,
                  season_gp: int = 30, season_min: float = 20.0,
                  recent_min: float = 0.0) -> dict:
    """Build a minimal player dict matching the shape compute_usage_adjustment() reads."""
    return {
        "playerId": player_id,
        "name": name,
        "usgPct": usg_pct,
        "status": status,
        "seasonGP": season_gp,
        "seasonMin": season_min,
        "recentMin": recent_min,
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

    def test_multiplier_capped_at_tier_limit(self, monkeypatch):
        """effectiveMultiplier is capped at the tier's cap (normal=1.45, extreme=2.00)."""
        # Two non-starters inactive (seasonMin < 28) → normal tier → cap 1.45
        players = [
            _make_player(1, "Target", usg_pct=20.0, status="Active"),
            _make_player(2, "Mega Star A", usg_pct=40.0, status="Inactive", season_min=25.0),
            _make_player(3, "Mega Star B", usg_pct=40.0, status="Inactive", season_min=25.0),
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


# ---------------------------------------------------------------------------
# Test 4: Mass-absence tier classification
# ---------------------------------------------------------------------------

class TestMassAbsenceTier:

    def test_normal_tier_0_absent_starters(self, monkeypatch):
        """No absent starters → normal tier."""
        players = [
            _make_player(1, "Target", usg_pct=22.0, status="Active", season_min=32.0),
            _make_player(2, "Teammate", usg_pct=24.0, status="Active", season_min=30.0),
        ]
        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )
        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        assert result["massAbsenceTier"] == "normal"
        assert result["absentStarterCount"] == 0

    def test_normal_tier_1_absent_starter(self, monkeypatch):
        """1 absent starter → still normal tier."""
        players = [
            _make_player(1, "Target", usg_pct=22.0, status="Active", season_min=32.0),
            _make_player(2, "Star Out", usg_pct=28.0, status="Inactive", season_min=34.0),
            _make_player(3, "Role Player", usg_pct=18.0, status="Active", season_min=22.0),
        ]
        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )
        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        assert result["massAbsenceTier"] == "normal"
        assert result["absentStarterCount"] == 1

    def test_moderate_tier_2_absent_starters(self, monkeypatch):
        """2 absent starters (seasonMin >= 28) → moderate tier."""
        players = [
            _make_player(1, "Target", usg_pct=22.0, status="Active", season_min=32.0),
            _make_player(2, "Star A Out", usg_pct=25.0, status="Inactive", season_min=30.0),
            _make_player(3, "Star B Out", usg_pct=23.0, status="Inactive", season_min=29.0),
            _make_player(4, "Role Player", usg_pct=15.0, status="Active", season_min=20.0),
        ]
        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )
        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        assert result["massAbsenceTier"] == "moderate"
        assert result["absentStarterCount"] == 2

    def test_extreme_tier_3_absent_starters(self, monkeypatch):
        """3 absent starters → extreme tier with higher cap."""
        players = [
            _make_player(1, "Target", usg_pct=22.0, status="Active", season_min=32.0),
            _make_player(2, "Star A", usg_pct=25.0, status="Inactive", season_min=33.0),
            _make_player(3, "Star B", usg_pct=24.0, status="Inactive", season_min=30.0),
            _make_player(4, "Star C", usg_pct=20.0, status="Inactive", season_min=29.0),
            _make_player(5, "Bench A", usg_pct=13.0, status="Active", season_min=15.0),
        ]
        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )
        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        assert result["massAbsenceTier"] == "extreme"
        assert result["absentStarterCount"] == 3
        # Extreme tier cap is 2.00
        assert result["effectiveMultiplier"] <= 2.00

    def test_extreme_tier_lower_usg_threshold(self, monkeypatch):
        """Extreme tier uses lower usg threshold (12%) — picks up lower-usage absent players."""
        players = [
            _make_player(1, "Target", usg_pct=22.0, status="Active", season_min=32.0),
            _make_player(2, "Star A", usg_pct=25.0, status="Inactive", season_min=33.0),
            _make_player(3, "Star B", usg_pct=24.0, status="Inactive", season_min=30.0),
            _make_player(4, "Star C", usg_pct=20.0, status="Inactive", season_min=29.0),
            # 14% usg teammate — below normal threshold (18%) but above extreme (12%)
            _make_player(5, "Mid Player", usg_pct=14.0, status="Inactive", season_min=24.0),
            _make_player(6, "Bench", usg_pct=10.0, status="Active", season_min=15.0),
        ]
        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )
        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        assert result["massAbsenceTier"] == "extreme"
        # 14% usg player should be included in absent list (>= 12% extreme threshold)
        absent_names = [t["name"] for t in result["absentTeammates"]]
        assert "Mid Player" in absent_names

    def test_non_starter_inactive_not_counted_for_tier(self, monkeypatch):
        """Players with seasonMin < 28 are NOT counted as absent starters for tier."""
        players = [
            _make_player(1, "Target", usg_pct=22.0, status="Active", season_min=32.0),
            # High usage but bench player (seasonMin 20) — doesn't count for tier
            _make_player(2, "Bench Star A", usg_pct=28.0, status="Inactive", season_min=20.0),
            _make_player(3, "Bench Star B", usg_pct=26.0, status="Inactive", season_min=22.0),
            _make_player(4, "Bench Star C", usg_pct=25.0, status="Inactive", season_min=18.0),
        ]
        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )
        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        # All are bench players (seasonMin < 28) — still normal tier
        assert result["massAbsenceTier"] == "normal"
        assert result["absentStarterCount"] == 0

    def test_recent_min_fallback_counts_as_starter(self, monkeypatch):
        """Deadline arrival: seasonMin < 28 but recentMin >= 28 counts as starter."""
        players = [
            _make_player(1, "Target", usg_pct=22.0, status="Active", season_min=32.0),
            # Traded mid-season: low seasonMin (split between two teams) but recent starter
            _make_player(2, "Trade A", usg_pct=24.0, status="Inactive",
                         season_min=22.0, recent_min=33.0),
            _make_player(3, "Trade B", usg_pct=23.0, status="Inactive",
                         season_min=20.0, recent_min=30.0),
        ]
        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )
        result = compute_usage_adjustment(player_id=1, team_abbr="MIN")
        assert result["success"] is True
        # recentMin >= 28 should classify them as starters → moderate tier
        assert result["massAbsenceTier"] == "moderate"
        assert result["absentStarterCount"] == 2

    def test_as_of_date_parameter_accepted(self, monkeypatch):
        """compute_usage_adjustment accepts as_of_date parameter."""
        players = [
            _make_player(1, "Target", usg_pct=22.0, status="Active", season_min=32.0),
        ]
        monkeypatch.setattr(
            "core.nba_prep_usage.get_team_roster_status",
            lambda team_abbr, season=None: _fake_roster_data(players),
        )
        result = compute_usage_adjustment(player_id=1, team_abbr="MIN", as_of_date="2026-02-15")
        assert result["success"] is True
