from contextlib import contextmanager

from nba_cli import scan_commands
from core import nba_line_store
from core import nba_decision_journal
from core import nba_model_training
from core import nba_data_collection
from core import nba_bet_tracking
from nba_api.stats.static import players as nba_players_static


def test_roster_sweep_duplicate_signal_still_backfills_prop_journal(monkeypatch):
    class FakeLineStore:
        def get_snapshots(self, date_str):
            return [
                {
                    "player_name": "Marcus Sasser",
                    "stat": "ast",
                    "book": "BetMGM",
                    "line": 4.5,
                    "over_odds": -110,
                    "under_odds": -120,
                    "player_team_abbr": "DET",
                    "opponent_abbr": "BKN",
                    "is_home": True,
                    "home_team_abbr": "DET",
                    "away_team_abbr": "BKN",
                    "commence_time": "2026-03-08T00:00:00Z",
                    "timestamp_utc": "2026-03-07T23:00:00Z",
                }
            ]

    class FakeJournal:
        def log_signal(self, **kwargs):
            return {"success": True, "isDuplicate": True}

        def log_lean(self, **kwargs):
            raise AssertionError("lean logging should not be used for qualifying signals")

        def close(self):
            return None

    captured = {}

    monkeypatch.setattr(nba_line_store, "LineStore", FakeLineStore)
    monkeypatch.setattr(nba_decision_journal, "DecisionJournal", FakeJournal)
    monkeypatch.setattr(nba_decision_journal, "_qualifies", lambda result, stat, used_real_line=True: (True, None))
    monkeypatch.setattr(nba_model_training, "american_to_implied_prob", lambda odds: 0.5)
    monkeypatch.setattr(
        nba_model_training,
        "compute_prop_ev",
        lambda **kwargs: {
            "success": True,
            "projection": {"projection": 1.2},
            "ev": {
                "probOver": 0.03,
                "probUnder": 0.97,
                "over": {"edge": -0.94},
                "under": {"edge": 0.77},
            },
            "referenceBook": None,
        },
    )
    monkeypatch.setattr(nba_data_collection, "safe_round", lambda val, digits=4: round(float(val), digits))
    monkeypatch.setattr(nba_data_collection, "get_yesterdays_team_abbrs", lambda date_str=None: set())
    monkeypatch.setattr(nba_data_collection, "get_todays_game_totals", lambda date_str=None: {})
    monkeypatch.setattr(nba_data_collection, "get_player_team_map", lambda max_age_sec=3600: {})
    monkeypatch.setattr(
        nba_bet_tracking,
        "log_prop_ev_entry",
        lambda prop_result, **kwargs: captured.update(
            {
                "prop_result": prop_result,
                "kwargs": kwargs,
            }
        )
        or {"success": True, "isDuplicate": False},
    )
    monkeypatch.setattr(
        nba_players_static,
        "find_players_by_full_name",
        lambda name: [{"id": 1631204, "full_name": "Marcus Sasser"}],
    )
    monkeypatch.setattr(nba_players_static, "get_players", lambda: [])

    @contextmanager
    def _fake_projection_context(use_local_projection_data):
        yield {"mode": "local_index"}

    monkeypatch.setattr(scan_commands, "_projection_data_context", _fake_projection_context)

    result = scan_commands._handle_roster_sweep(["nba_mod.py", "roster_sweep", "2026-03-07"])

    assert result["success"] is True
    assert result["logged"] == 0
    assert result["skipReasons"]["duplicate"] == 1
    assert captured["prop_result"]["commenceTime"] == "2026-03-08T00:00:00Z"
    assert captured["kwargs"]["player_id"] == 1631204
