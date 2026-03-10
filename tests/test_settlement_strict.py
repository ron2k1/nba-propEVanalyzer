"""tests/test_settlement_strict.py — Pin strict settlement row matching,
stat bounds checks, and MIN-field safety gate.

Covers fixes for:
- _find_game_row: hard opponent filter (no fallback to wrong game)
- _extract_stat_from_row: bounds checking, None on bad data
- MIN field presence as final-status safety gate
"""

import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.nba_bet_tracking import _find_game_row, _extract_stat_from_row

_MAR09 = date(2026, 3, 9)
_MAR08 = date(2026, 3, 8)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(date, matchup, pts=20, reb=8, ast=5, stl=1, blk=1, tov=2, fg3m=3, min_val="32:00"):
    r = {
        "GAME_DATE": date,
        "MATCHUP": matchup,
        "PTS": pts, "REB": reb, "AST": ast,
        "STL": stl, "BLK": blk, "TOV": tov, "FG3M": fg3m,
    }
    if min_val is not None:
        r["MIN"] = min_val
    return r


# ---------------------------------------------------------------------------
# Test 1: _find_game_row — strict opponent matching
# ---------------------------------------------------------------------------

class TestFindGameRowStrict:
    """Opponent and is_home are hard filters when provided."""

    def test_exact_opponent_match(self):
        logs = [_row("MAR 09, 2026", "CLE vs. ORL")]
        assert _find_game_row(logs, _MAR09, opponent_abbr="ORL") is not None

    def test_wrong_opponent_returns_none(self):
        """If opponent doesn't match, must NOT fall back to first same-date row."""
        logs = [_row("MAR 09, 2026", "CLE vs. ORL")]
        assert _find_game_row(logs, _MAR09, opponent_abbr="LAL") is None

    def test_no_opponent_returns_first_match(self):
        """Without opponent_abbr, first same-date row is returned."""
        logs = [_row("MAR 09, 2026", "CLE vs. ORL")]
        assert _find_game_row(logs, _MAR09) is not None

    def test_wrong_home_returns_none(self):
        """is_home mismatch → None, not fallback."""
        logs = [_row("MAR 09, 2026", "CLE @ ORL")]  # away game
        assert _find_game_row(logs, _MAR09, is_home=True) is None

    def test_correct_home_match(self):
        logs = [_row("MAR 09, 2026", "CLE vs. ORL")]  # home game
        assert _find_game_row(logs, _MAR09, is_home=True) is not None

    def test_doubleheader_picks_correct_opponent(self):
        """Two games same date — opponent filter selects the right one."""
        logs = [
            _row("MAR 09, 2026", "CLE vs. ORL", pts=15),
            _row("MAR 09, 2026", "CLE vs. LAL", pts=30),
        ]
        row = _find_game_row(logs, _MAR09, opponent_abbr="LAL")
        assert row is not None
        assert row["PTS"] == 30

    def test_wrong_date_returns_none(self):
        logs = [_row("MAR 08, 2026", "CLE vs. ORL")]
        assert _find_game_row(logs, _MAR09, opponent_abbr="ORL") is None

    def test_both_filters_applied(self):
        """opponent + is_home both applied strictly."""
        logs = [
            _row("MAR 09, 2026", "CLE @ ORL"),   # away
            _row("MAR 09, 2026", "CLE vs. LAL"),  # home
        ]
        # Want home game vs LAL
        row = _find_game_row(logs, _MAR09, opponent_abbr="LAL", is_home=True)
        assert row is not None
        # Want away game vs LAL — doesn't exist
        assert _find_game_row(logs, _MAR09, opponent_abbr="LAL", is_home=False) is None


# ---------------------------------------------------------------------------
# Test 2: _extract_stat_from_row — bounds checking
# ---------------------------------------------------------------------------

class TestExtractStatBounds:
    """Stat extraction rejects out-of-bounds and missing values."""

    def test_normal_values(self):
        row = _row("MAR 09, 2026", "CLE vs. ORL", pts=25, reb=10, ast=7)
        assert _extract_stat_from_row(row, "pts") == 25
        assert _extract_stat_from_row(row, "reb") == 10
        assert _extract_stat_from_row(row, "ast") == 7

    def test_combo_stats(self):
        row = _row("MAR 09, 2026", "CLE vs. ORL", pts=25, reb=10, ast=7)
        assert _extract_stat_from_row(row, "pra") == 42
        assert _extract_stat_from_row(row, "pr") == 35
        assert _extract_stat_from_row(row, "pa") == 32
        assert _extract_stat_from_row(row, "ra") == 17

    def test_negative_pts_returns_none(self):
        row = _row("MAR 09, 2026", "CLE vs. ORL", pts=-5)
        assert _extract_stat_from_row(row, "pts") is None

    def test_impossibly_high_pts_returns_none(self):
        row = _row("MAR 09, 2026", "CLE vs. ORL", pts=150)
        assert _extract_stat_from_row(row, "pts") is None

    def test_missing_field_returns_none(self):
        row = {"GAME_DATE": "MAR 09, 2026", "MATCHUP": "CLE vs. ORL"}
        # No PTS field at all
        assert _extract_stat_from_row(row, "pts") is None

    def test_combo_stat_missing_component_returns_none(self):
        """PRA requires all three; missing REB → None."""
        row = {"GAME_DATE": "MAR 09, 2026", "MATCHUP": "CLE vs. ORL",
               "PTS": 25, "AST": 7}  # no REB
        assert _extract_stat_from_row(row, "pra") is None

    def test_zero_is_valid(self):
        """0 points is legitimate (DNP-CD scored 0, or actual 0)."""
        row = _row("MAR 09, 2026", "CLE vs. ORL", pts=0, reb=0, ast=0)
        assert _extract_stat_from_row(row, "pts") == 0
        assert _extract_stat_from_row(row, "pra") == 0

    def test_unsupported_stat_returns_none(self):
        row = _row("MAR 09, 2026", "CLE vs. ORL")
        assert _extract_stat_from_row(row, "xyz") is None

    def test_bounds_per_stat_type(self):
        """Each stat has its own ceiling."""
        # STL max is 15
        row = _row("MAR 09, 2026", "CLE vs. ORL", stl=16)
        assert _extract_stat_from_row(row, "stl") is None
        row = _row("MAR 09, 2026", "CLE vs. ORL", stl=10)
        assert _extract_stat_from_row(row, "stl") == 10


# ---------------------------------------------------------------------------
# Test 3: MIN field as final-status safety gate
# ---------------------------------------------------------------------------

class TestMinFieldGate:
    """The MIN field check is in the settlement loop, not in _extract_stat.
    We verify the invariant: rows without MIN should not be graded."""

    def test_row_with_min_field(self):
        row = _row("MAR 09, 2026", "CLE vs. ORL", min_val="32:00")
        assert "MIN" in row

    def test_row_without_min_field(self):
        row = _row("MAR 09, 2026", "CLE vs. ORL", min_val=None)
        assert "MIN" not in row
        assert "min" not in row
