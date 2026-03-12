"""
tests/test_prop_engine.py — Wave 5 coverage for core/nba_prop_engine.py.

Tests the public helper functions and internal utilities of the prop engine
module WITHOUT requiring network access or real NBA API calls.

Covers:
- Book priority ordering and normalization
- Line matching via _best_side_prices_for_line
- compute_prop_ev return-value contract (mocked projection + EV)
- no_blend default parameter
- compute_live_projection arithmetic
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.nba_prop_engine import (
    _book_priority_score,
    _normalize_book_key,
    _best_side_prices_for_line,
    _clean_bookmaker_csv,
    compute_prop_ev,
    compute_live_projection,
    PREFERRED_BOOKMAKERS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_offer(bookmaker, line, over_odds, under_odds):
    """Build a minimal offer dict matching the shape expected by the engine."""
    return {
        "bookmaker": bookmaker,
        "line": line,
        "overOdds": over_odds,
        "underOdds": under_odds,
    }


def _make_projection_result(stat="pts", projection=25.0, stdev=5.0):
    """Build a successful projection result dict."""
    return {
        "success": True,
        "projections": {
            stat: {
                "projection": projection,
                "projStdev": stdev,
                "seasonAvg": projection - 1.0,
                "nGames": 40,
                "confidence": 0.72,
                "shrinkWeight": 0.65,
            },
        },
        "matchupHistory": None,
        "opponentDefense": None,
        "position": "G",
        "gamesPlayed": 40,
        "modelVariant": "full",
        "minutesProjection": {"projectedMinutes": 35.0},
        "usageAdjustment": None,
        "starReplacementFlag": False,
    }


# ---------------------------------------------------------------------------
# TestPropEngineSweep
# ---------------------------------------------------------------------------

class TestPropEngineSweep(unittest.TestCase):
    """Test compute_prop_ev return contract and configuration defaults."""

    @patch("core.nba_prop_engine.compute_projection")
    @patch("core.nba_prop_engine.get_nba_player_prop_offers")
    def test_compute_prop_ev_returns_expected_keys(self, mock_offers, mock_proj):
        """compute_prop_ev() result dict must contain all documented keys."""
        mock_proj.return_value = _make_projection_result("pts", 25.0, 5.0)
        mock_offers.return_value = {"success": False}  # skip market refresh

        result = compute_prop_ev(
            player_id=1630162,
            opponent_abbr="ORL",
            is_home=1,
            stat="pts",
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            refresh_market_offers=False,
        )

        self.assertTrue(result.get("success"), f"Expected success, got: {result}")

        expected_keys = {
            "success", "stat", "line", "projection", "ev",
            "matchupHistory", "opponentDefense", "position",
            "gamesPlayed", "playerId", "opponent", "isHome",
            "isB2B", "modelVariant", "minutesProjection",
            "bestOverOdds", "bestUnderOdds", "bestOverBook",
            "bestUnderBook", "lineShopping", "referenceBook",
            "usageAdjustment", "impliedProjection", "modelMarketDelta",
            "commenceTime", "outcomeModelWinProb", "starReplacementFlag",
        }
        missing = expected_keys - set(result.keys())
        self.assertEqual(missing, set(), f"Missing keys: {missing}")

    @patch("core.nba_prop_engine.compute_projection")
    def test_compute_prop_ev_with_stat_param(self, mock_proj):
        """stat= must be forwarded to compute_ev so calibration applies."""
        mock_proj.return_value = _make_projection_result("pts", 25.0, 5.0)

        with patch("core.nba_prop_engine.compute_ev") as mock_ev:
            mock_ev.return_value = {
                "probOver": 0.45,
                "probUnder": 0.55,
                "distributionMode": "normal_cdf",
                "over": {"edge": 0.05, "evPercent": 3.2, "verdict": "Thin Edge"},
                "under": {"edge": 0.08, "evPercent": 7.1, "verdict": "Good Value"},
            }

            compute_prop_ev(
                player_id=1630162,
                opponent_abbr="ORL",
                is_home=1,
                stat="pts",
                line=25.5,
                over_odds=-110,
                under_odds=-110,
                refresh_market_offers=False,
            )

            mock_ev.assert_called_once()
            call_kwargs = mock_ev.call_args
            # stat= is passed as a keyword argument
            _, kwargs = call_kwargs
            self.assertEqual(kwargs.get("stat"), "pts",
                             "compute_ev must receive stat='pts' for calibration")

    def test_book_priority_ordering(self):
        """BetMGM > DraftKings > FanDuel > unknown (per CLAUDE.md)."""
        betmgm = _book_priority_score("BetMGM")
        dk = _book_priority_score("DraftKings")
        fd = _book_priority_score("FanDuel")
        unknown = _book_priority_score("PointsBet")

        self.assertGreater(betmgm, dk)
        self.assertGreater(dk, fd)
        self.assertGreater(fd, unknown)
        self.assertEqual(unknown, 0)

    @patch("core.nba_prop_engine.compute_projection")
    def test_no_blend_default(self, mock_proj):
        """no_blend defaults to True (disabled blend per 2026-03-03 decision)."""
        mock_proj.return_value = _make_projection_result("pts", 25.0, 5.0)

        compute_prop_ev(
            player_id=1630162,
            opponent_abbr="ORL",
            is_home=1,
            stat="pts",
            line=25.5,
            over_odds=-110,
            under_odds=-110,
            refresh_market_offers=False,
        )

        # When no_blend=True (default), blend_with_line should be None
        call_kwargs = mock_proj.call_args
        _, kwargs = call_kwargs
        self.assertIsNone(
            kwargs.get("blend_with_line"),
            "no_blend=True should pass blend_with_line=None to compute_projection",
        )

    def test_preferred_bookmakers_tuple(self):
        """PREFERRED_BOOKMAKERS must be a tuple in the expected order."""
        self.assertIsInstance(PREFERRED_BOOKMAKERS, tuple)
        self.assertEqual(
            PREFERRED_BOOKMAKERS,
            ("betmgm", "draftkings", "fanduel"),
        )


# ---------------------------------------------------------------------------
# TestLineMatching
# ---------------------------------------------------------------------------

class TestLineMatching(unittest.TestCase):
    """Test _best_side_prices_for_line line-matching logic."""

    def test_line_matching_exact(self):
        """Exact line match returns the correct over/under prices."""
        offers = [
            _make_offer("betmgm", 25.5, -110, -110),
            _make_offer("draftkings", 25.5, -108, -112),
        ]
        best_over, best_under = _best_side_prices_for_line(offers, 25.5)

        # DraftKings -108 over is better than BetMGM -110 over
        self.assertIsNotNone(best_over)
        self.assertEqual(best_over["book"], "draftkings")
        self.assertEqual(best_over["odds"], -108)

        # BetMGM -110 under is better than DraftKings -112 under
        self.assertIsNotNone(best_under)
        self.assertEqual(best_under["book"], "betmgm")
        self.assertEqual(best_under["odds"], -110)

    def test_line_matching_nearest_within_tolerance(self):
        """Offers within tolerance are matched; outside tolerance are excluded."""
        offers = [
            _make_offer("betmgm", 25.5, -110, -110),
            _make_offer("fanduel", 26.5, -105, -115),  # 1.0 away — beyond default tolerance
        ]
        # Default tolerance is 0.05, so only 25.5 matches target 25.5
        best_over, best_under = _best_side_prices_for_line(offers, 25.5, tolerance=0.05)
        self.assertIsNotNone(best_over)
        self.assertEqual(best_over["line"], 25.5)

        # FanDuel at 26.5 should NOT match
        best_over_wide, _ = _best_side_prices_for_line(offers, 26.0, tolerance=0.05)
        self.assertIsNone(best_over_wide)

    def test_line_matching_empty_offers(self):
        """Empty or None offers list returns (None, None)."""
        self.assertEqual(_best_side_prices_for_line([], 25.5), (None, None))
        self.assertEqual(_best_side_prices_for_line(None, 25.5), (None, None))

    def test_line_matching_tiebreak_by_book_priority(self):
        """When decimal odds are identical, higher-priority book wins."""
        offers = [
            _make_offer("fanduel", 25.5, -110, -110),
            _make_offer("betmgm", 25.5, -110, -110),
        ]
        best_over, best_under = _best_side_prices_for_line(offers, 25.5)

        # Same odds → BetMGM wins on priority
        self.assertEqual(best_over["book"], "betmgm")
        self.assertEqual(best_under["book"], "betmgm")

    def test_line_matching_best_decimal_wins(self):
        """Better decimal odds should beat higher book priority."""
        offers = [
            _make_offer("betmgm", 25.5, -115, -105),
            _make_offer("fanduel", 25.5, -105, -115),
        ]
        best_over, best_under = _best_side_prices_for_line(offers, 25.5)

        # FanDuel -105 over > BetMGM -115 over (decimal: 1.952 > 1.870)
        self.assertEqual(best_over["book"], "fanduel")
        self.assertEqual(best_over["odds"], -105)

        # BetMGM -105 under > FanDuel -115 under
        self.assertEqual(best_under["book"], "betmgm")
        self.assertEqual(best_under["odds"], -105)


# ---------------------------------------------------------------------------
# TestBookNormalization
# ---------------------------------------------------------------------------

class TestBookNormalization(unittest.TestCase):
    """Test _normalize_book_key and _clean_bookmaker_csv utilities."""

    def test_normalize_strips_non_alnum(self):
        self.assertEqual(_normalize_book_key("Bet MGM"), "betmgm")
        self.assertEqual(_normalize_book_key("Draft-Kings"), "draftkings")
        self.assertEqual(_normalize_book_key("FAN_DUEL"), "fanduel")

    def test_normalize_none_input(self):
        self.assertEqual(_normalize_book_key(None), "")

    def test_clean_bookmaker_csv_normal(self):
        result = _clean_bookmaker_csv("BetMGM, DraftKings , FanDuel")
        self.assertEqual(result, "betmgm,draftkings,fanduel")

    def test_clean_bookmaker_csv_empty_uses_default(self):
        result = _clean_bookmaker_csv("", default_csv="betmgm,draftkings")
        self.assertEqual(result, "betmgm,draftkings")

    def test_clean_bookmaker_csv_none_uses_default(self):
        result = _clean_bookmaker_csv(None, default_csv="fanduel")
        self.assertEqual(result, "fanduel")


# ---------------------------------------------------------------------------
# TestLiveProjection
# ---------------------------------------------------------------------------

class TestLiveProjection(unittest.TestCase):
    """Test compute_live_projection arithmetic and edge cases."""

    def test_basic_live_projection(self):
        """Mid-game projection blends pregame and live rates."""
        pregame = {
            "projection": 25.0,
            "projectedMinutes": 36.0,
            "perMinRate": 25.0 / 36.0,
        }
        live = {
            "PTS": 10,
            "minsPlayed": 18.0,
            "period": 2,
            "scoreMargin": 3,
            "PF": 1,
            "FGA": 8,
            "FTA": 4,
        }
        result = compute_live_projection(pregame, live, "pts")

        self.assertTrue(result["success"])
        self.assertEqual(result["stat"], "pts")
        self.assertGreater(result["liveProjection"], 0)
        self.assertEqual(result["currentStat"], 10)
        self.assertEqual(result["minsPlayed"], 18.0)

    def test_live_projection_zero_minutes(self):
        """Before the game starts, live rate falls back to pregame rate."""
        pregame = {
            "projection": 20.0,
            "projectedMinutes": 32.0,
            "perMinRate": 20.0 / 32.0,
        }
        live = {
            "REB": 0,
            "minsPlayed": 0.0,
            "period": 1,
            "scoreMargin": 0,
            "PF": 0,
        }
        result = compute_live_projection(pregame, live, "reb")

        self.assertTrue(result["success"])
        # With 0 minutes played, blend_weight should be 0 → pure pregame rate
        self.assertEqual(result["blendWeight"], 0.0)
        self.assertAlmostEqual(result["liveProjection"], 20.0, places=1)

    def test_live_projection_close_game_floor(self):
        """Close-game floor boosts remaining minutes for starters in late game."""
        pregame = {
            "projection": 28.0,
            "projectedMinutes": 34.0,
            "perMinRate": 28.0 / 34.0,
        }
        live = {
            "PTS": 20,
            "minsPlayed": 30.0,
            "period": 4,
            "scoreMargin": 3,  # close game
            "PF": 2,
            "FGA": 15,
            "FTA": 6,
        }
        result = compute_live_projection(pregame, live, "pts")

        self.assertTrue(result["success"])
        # Close game in Q4 with 30 min played should trigger floor
        # projectedMinutes >= 28, period >= 3, margin <= 10, fouls < 5
        self.assertTrue(result["closeGameFloor"])

    def test_live_projection_blowout_trims_minutes(self):
        """Blowout in Q3+ reduces remaining minutes via multiplier < 1."""
        pregame = {
            "projection": 25.0,
            "projectedMinutes": 36.0,
            "perMinRate": 25.0 / 36.0,
        }
        live = {
            "AST": 5,
            "minsPlayed": 24.0,
            "period": 3,
            "scoreMargin": 20,  # blowout
            "PF": 1,
        }
        result = compute_live_projection(pregame, live, "ast")

        self.assertTrue(result["success"])
        # margin >= 15 in period >= 3 → multiplier 0.90
        self.assertAlmostEqual(result["minuteMultiplier"], 0.90, places=2)


# ---------------------------------------------------------------------------
# compute_auto_line_sweep contract tests (monkeypatched, no network)
# ---------------------------------------------------------------------------

class TestAutoLineSweepContract(unittest.TestCase):
    """Test compute_auto_line_sweep() return-value contract with mocked dependencies."""

    def _make_projection_data(self, stat="pts", projection=25.0, stdev=5.0):
        return {
            "success": True,
            "projections": {
                stat: {
                    "projection": projection,
                    "projStdev": stdev,
                    "seasonAvg": 24.0,
                    "nGames": 30,
                }
            },
            "gamesPlayed": 30,
            "minutesProjection": {"projectedMinutes": 34.0},
        }

    def _make_offer_data(self, lines=None):
        if lines is None:
            lines = [
                {"line": 24.5, "overOdds": -110, "underOdds": -110, "bookmaker": "betmgm"},
                {"line": 24.5, "overOdds": -115, "underOdds": -105, "bookmaker": "draftkings"},
            ]
        return {
            "success": True,
            "offers": lines,
            "eventId": "test-event-1",
            "eventHomeTeam": "MIN",
            "eventAwayTeam": "ORL",
            "commenceTime": "2026-03-11T23:30:00Z",
            "marketKey": "player_points",
            "quota": {"used": 1, "remaining": 499},
            "bookmakersRequested": "betmgm,draftkings",
        }

    @patch("core.nba_prop_engine.get_nba_player_prop_offers")
    @patch("core.nba_prop_engine.compute_projection")
    @patch("core.nba_prop_engine._ALL_PLAYERS_BY_ID", {1630162: "Anthony Edwards"})
    def test_sweep_success_return_keys(self, mock_proj, mock_offers):
        """Successful sweep should return all documented keys."""
        from core.nba_prop_engine import compute_auto_line_sweep

        mock_proj.return_value = self._make_projection_data()
        mock_offers.return_value = self._make_offer_data()

        result = compute_auto_line_sweep(
            player_id=1630162,
            player_team_abbr="MIN",
            opponent_abbr="ORL",
            is_home=True,
            stat="pts",
        )

        self.assertTrue(result["success"])
        # Verify all documented return keys exist
        required_keys = [
            "success", "playerId", "playerName", "playerTeamAbbr",
            "opponentAbbr", "isHome", "stat", "projection",
            "projectionValue", "offerCount", "rankedOffers",
            "bestRecommendation", "nBooksOffering", "bookLineStdev",
        ]
        for key in required_keys:
            self.assertIn(key, result, f"Missing key: {key}")

        # Verify rankedOffers structure
        self.assertGreater(len(result["rankedOffers"]), 0)
        offer = result["rankedOffers"][0]
        offer_keys = [
            "bookmaker", "line", "overOdds", "underOdds",
            "bestSide", "bestEvPct", "probOver", "probUnder",
            "edgeOver", "edgeUnder", "overVerdict", "underVerdict",
            "vigSpread", "ev",
        ]
        for key in offer_keys:
            self.assertIn(key, offer, f"Missing offer key: {key}")

    @patch("core.nba_prop_engine.compute_projection")
    @patch("core.nba_prop_engine._ALL_PLAYERS_BY_ID", {1630162: "Anthony Edwards"})
    def test_sweep_projection_failure(self, mock_proj):
        """Failed projection should return success=False without calling offers API."""
        from core.nba_prop_engine import compute_auto_line_sweep

        mock_proj.return_value = {"success": False, "error": "NBA API down"}

        result = compute_auto_line_sweep(
            player_id=1630162,
            player_team_abbr="MIN",
            opponent_abbr="ORL",
            is_home=True,
            stat="pts",
        )

        self.assertFalse(result["success"])

    @patch("core.nba_prop_engine.get_nba_player_prop_offers")
    @patch("core.nba_prop_engine.compute_projection")
    @patch("core.nba_prop_engine._ALL_PLAYERS_BY_ID", {1630162: "Anthony Edwards"})
    def test_sweep_no_offers(self, mock_proj, mock_offers):
        """No offers found should return success=False with offerCount=0."""
        from core.nba_prop_engine import compute_auto_line_sweep

        mock_proj.return_value = self._make_projection_data()
        mock_offers.return_value = {
            "success": True,
            "offers": [],
            "eventId": "test-event-1",
            "eventHomeTeam": "MIN",
            "eventAwayTeam": "ORL",
        }

        result = compute_auto_line_sweep(
            player_id=1630162,
            player_team_abbr="MIN",
            opponent_abbr="ORL",
            is_home=True,
            stat="pts",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["offerCount"], 0)

    @patch("core.nba_prop_engine.get_nba_player_prop_offers")
    @patch("core.nba_prop_engine.compute_projection")
    @patch("core.nba_prop_engine._ALL_PLAYERS_BY_ID", {1630162: "Anthony Edwards"})
    def test_sweep_ranking_order(self, mock_proj, mock_offers):
        """Ranked offers should be sorted by bestEvPct descending."""
        from core.nba_prop_engine import compute_auto_line_sweep

        mock_proj.return_value = self._make_projection_data(projection=30.0)
        mock_offers.return_value = self._make_offer_data(lines=[
            {"line": 24.5, "overOdds": -110, "underOdds": -110, "bookmaker": "betmgm"},
            {"line": 25.5, "overOdds": -110, "underOdds": -110, "bookmaker": "draftkings"},
            {"line": 26.5, "overOdds": -110, "underOdds": -110, "bookmaker": "fanduel"},
        ])

        result = compute_auto_line_sweep(
            player_id=1630162,
            player_team_abbr="MIN",
            opponent_abbr="ORL",
            is_home=True,
            stat="pts",
        )

        self.assertTrue(result["success"])
        evs = [o["bestEvPct"] for o in result["rankedOffers"]]
        self.assertEqual(evs, sorted(evs, reverse=True),
                         "Offers should be ranked by EV descending")

    @patch("core.nba_prop_engine.get_nba_player_prop_offers")
    @patch("core.nba_prop_engine.compute_projection")
    @patch("core.nba_prop_engine._ALL_PLAYERS_BY_ID", {1630162: "Anthony Edwards"})
    def test_sweep_passes_stat_to_compute_ev(self, mock_proj, mock_offers):
        """compute_ev inside the sweep must receive stat= for calibration."""
        from core.nba_prop_engine import compute_auto_line_sweep

        mock_proj.return_value = self._make_projection_data()
        mock_offers.return_value = self._make_offer_data()

        result = compute_auto_line_sweep(
            player_id=1630162,
            player_team_abbr="MIN",
            opponent_abbr="ORL",
            is_home=True,
            stat="pts",
        )

        self.assertTrue(result["success"])
        # The EV inside each ranked offer should have distributionMode
        # (proving compute_ev was called, not skipped)
        ev = result["rankedOffers"][0]["ev"]
        self.assertIn("distributionMode", ev)
        self.assertEqual(ev["distributionMode"], "normal",
                         "pts should use normal distribution")


if __name__ == "__main__":
    unittest.main()
