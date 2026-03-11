from pathlib import Path

from core import nba_bet_tracking as bt
from core import nba_data_collection as dc


def _make_entry(*, entry_id: str, created_at: str, line: float = 10.5):
    return {
        "entryId": entry_id,
        "createdAtUtc": created_at,
        "pickDate": "2026-03-05",
        "playerId": 1642272,
        "opponentAbbr": "DET",
        "isHome": True,
        "isB2B": False,
        "stat": "pts",
        "line": line,
        "overOdds": -110,
        "underOdds": -110,
    }


def _scratch_journal_path():
    return Path(__file__).resolve().parents[1] / "data" / "_test_prop_journal.jsonl"


def test_append_journal_entry_replaces_duplicate_key_with_latest(monkeypatch):
    journal_path = _scratch_journal_path()
    journal_path.unlink(missing_ok=True)
    monkeypatch.setattr(bt, "DATA_DIR", journal_path.parent)
    monkeypatch.setattr(bt, "JOURNAL_PATH", journal_path)

    first = _make_entry(
        entry_id="first",
        created_at="2026-03-05T18:00:00Z",
    )
    second = _make_entry(
        entry_id="second",
        created_at="2026-03-05T18:05:00Z",
    )

    first_result = bt._append_journal_entry(first)
    second_result = bt._append_journal_entry(second)
    entries = bt._load_journal_entries()

    assert first_result["isDuplicate"] is False
    assert second_result["isDuplicate"] is True
    assert len(entries) == 1
    assert entries[0]["entryId"] == "second"
    assert entries[0]["createdAtUtc"] == "2026-03-05T18:05:00Z"
    journal_path.unlink(missing_ok=True)


def test_append_journal_entry_line_differ_dedup_keeps_latest(monkeypatch):
    """Same (date, player, opponent, home, b2b, stat) at different lines
    should be treated as a duplicate — only the latest entry survives."""
    journal_path = _scratch_journal_path()
    journal_path.unlink(missing_ok=True)
    monkeypatch.setattr(bt, "DATA_DIR", journal_path.parent)
    monkeypatch.setattr(bt, "JOURNAL_PATH", journal_path)

    first = _make_entry(
        entry_id="first",
        created_at="2026-03-05T18:00:00Z",
        line=9.5,
    )
    second = _make_entry(
        entry_id="second",
        created_at="2026-03-05T18:05:00Z",
        line=10.5,
    )

    first_result = bt._append_journal_entry(first)
    second_result = bt._append_journal_entry(second)
    entries = bt._load_journal_entries()

    assert first_result["isDuplicate"] is False
    assert second_result["isDuplicate"] is True
    assert len(entries) == 1
    assert entries[0]["entryId"] == "second"
    assert entries[0]["createdAtUtc"] == "2026-03-05T18:05:00Z"
    journal_path.unlink(missing_ok=True)


def test_opponent_differ_dedup_keeps_latest(monkeypatch):
    """Same (date, player, stat) but different opponent should be treated as a
    duplicate — a player can only play one game per date.  Latest wins."""
    journal_path = _scratch_journal_path()
    journal_path.unlink(missing_ok=True)
    monkeypatch.setattr(bt, "DATA_DIR", journal_path.parent)
    monkeypatch.setattr(bt, "JOURNAL_PATH", journal_path)

    first = {
        **_make_entry(entry_id="stale-opp", created_at="2026-03-05T17:00:00Z"),
        "opponentAbbr": "DEN",
        "isHome": False,
    }
    second = {
        **_make_entry(entry_id="correct-opp", created_at="2026-03-05T18:00:00Z"),
        "opponentAbbr": "DET",
        "isHome": True,
    }

    first_result = bt._append_journal_entry(first)
    second_result = bt._append_journal_entry(second)
    entries = bt._load_journal_entries()

    assert first_result["isDuplicate"] is False
    assert second_result["isDuplicate"] is True
    assert len(entries) == 1
    assert entries[0]["entryId"] == "correct-opp"
    assert entries[0]["opponentAbbr"] == "DET"
    journal_path.unlink(missing_ok=True)


def test_dedup_journal_collapses_cleanup_key_duplicates(monkeypatch):
    journal_path = _scratch_journal_path()
    journal_path.unlink(missing_ok=True)
    monkeypatch.setattr(bt, "DATA_DIR", journal_path.parent)
    monkeypatch.setattr(bt, "JOURNAL_PATH", journal_path)

    first = {
        **_make_entry(entry_id="first", created_at="2026-03-05T18:00:00Z", line=10.5),
        "playerName": "Marcus Sasser",
        "overOdds": -110,
        "underOdds": -110,
    }
    second = {
        **_make_entry(entry_id="second", created_at="2026-03-05T18:05:00Z", line=10.5),
        "playerName": "Marcus Sasser",
        "overOdds": -115,
        "underOdds": -105,
    }

    bt._write_journal_entries([first, second])
    result = bt.dedup_journal()
    entries = bt._load_journal_entries()

    assert result["success"] is True
    assert result["removedCount"] == 1
    assert len(entries) == 1
    assert entries[0]["entryId"] == "second"
    journal_path.unlink(missing_ok=True)


def test_get_playing_teams_today_falls_back_to_line_history(monkeypatch):
    monkeypatch.setattr(dc, "get_todays_games", lambda game_date=None: {"games": []})
    monkeypatch.setattr(
        bt,
        "_load_line_history",
        lambda target, phase=None: {
            ("marcus sasser", "ast"): [
                {"home_team_abbr": "DET", "away_team_abbr": "BKN"},
            ]
        },
    )

    teams = bt._get_playing_teams_today("2026-03-07")

    assert teams == {"DET", "BKN"}


def test_best_plays_for_date_merges_jsonl_and_sqlite_entries(monkeypatch):
    jsonl_entry = {
        "entryId": "jsonl-1",
        "createdAtUtc": "2026-03-07T18:18:00Z",
        "createdAtLocal": "2026-03-07T18:18:00",
        "pickDate": "2026-03-07",
        "playerId": 1631204,
        "playerName": "Marcus Sasser",
        "playerTeamAbbr": "DET",
        "opponentAbbr": "BKN",
        "isHome": True,
        "isB2B": False,
        "stat": "ast",
        "line": 4.5,
        "overOdds": -110,
        "underOdds": -120,
        "recommendedSide": "under",
        "recommendedEvPct": 77.8,
        "recommendedOdds": -120,
        "probOver": 0.0302,
        "probUnder": 0.9698,
        "projection": 1.2,
        "settled": False,
        "result": None,
    }
    sqlite_duplicate = {
        "pickDate": "2026-03-07",
        "playerId": 1631204,
        "playerName": "Marcus Sasser",
        "playerTeamAbbr": "DET",
        "opponentAbbr": "BKN",
        "isHome": True,
        "isB2B": False,
        "stat": "ast",
        "line": 4.5,
        "overOdds": -110,
        "underOdds": -120,
        "recommendedSide": "under",
        "recommendedEvPct": 52.0,
        "recommendedOdds": -120,
        "probOver": 0.05,
        "probUnder": 0.95,
        "projection": 1.3,
        "settled": False,
        "source": "sqlite_fallback",
    }
    sqlite_unique = {
        "pickDate": "2026-03-07",
        "playerId": 1642450,
        "playerName": "Daniss Jenkins",
        "playerTeamAbbr": "DET",
        "opponentAbbr": "BKN",
        "isHome": False,
        "isB2B": False,
        "stat": "ast",
        "line": 5.5,
        "overOdds": -110,
        "underOdds": -118,
        "recommendedSide": "under",
        "recommendedEvPct": 42.06,
        "recommendedOdds": -118,
        "probOver": 0.0712,
        "probUnder": 0.9288,
        "projection": 1.3,
        "settled": False,
        "source": "sqlite_fallback",
    }

    monkeypatch.setattr(bt, "_load_journal_entries", lambda: [jsonl_entry])
    monkeypatch.setattr(bt, "_sqlite_fallback_entries", lambda target: [sqlite_duplicate, sqlite_unique])
    monkeypatch.setattr(bt, "_get_playing_teams_today", lambda target_date=None: {"DET", "BKN"})
    monkeypatch.setattr(bt, "_load_line_history", lambda target, phase=None: {})

    result = bt.best_plays_for_date("2026-03-07", limit=10)

    assert result["success"] is True
    assert result["totalRanked"] == 2
    assert result["entriesLogged"] == 2
    assert [row["playerName"] for row in result["topOffers"]] == [
        "Marcus Sasser",
        "Daniss Jenkins",
    ]
    assert result["topOffers"][0]["entryId"] == "jsonl-1"


def test_jsonl_entry_preserves_swept_at_utc(monkeypatch):
    """sweptAtUtc written to JSONL must surface in best_plays_for_date output."""
    entry = {
        "entryId": "swept-1",
        "createdAtUtc": "2026-03-09T20:00:00Z",
        "createdAtLocal": "2026-03-09T14:00:00",
        "pickDate": "2026-03-09",
        "playerId": 1631204,
        "playerName": "Marcus Sasser",
        "playerTeamAbbr": "DET",
        "opponentAbbr": "BKN",
        "isHome": True,
        "isB2B": False,
        "stat": "pts",
        "line": 10.5,
        "overOdds": -110,
        "underOdds": -110,
        "recommendedSide": "over",
        "recommendedEvPct": 12.5,
        "recommendedOdds": -110,
        "probOver": 0.62,
        "probUnder": 0.38,
        "projection": 12.0,
        "settled": False,
        "result": None,
        "sweptAtUtc": "2026-03-09T19:58:30Z",
    }
    monkeypatch.setattr(bt, "_load_journal_entries", lambda: [entry])
    monkeypatch.setattr(bt, "_sqlite_fallback_entries", lambda target: [])
    monkeypatch.setattr(bt, "_get_playing_teams_today", lambda target_date=None: {"DET", "BKN"})
    monkeypatch.setattr(bt, "_load_line_history", lambda target, phase=None: {})
    monkeypatch.setattr(bt, "_get_pulled_players", lambda target_date: set())

    result = bt.best_plays_for_date("2026-03-09", limit=5)
    assert result["success"] is True
    row = result["topOffers"][0]
    assert row["sweptAtUtc"] == "2026-03-09T19:58:30Z"


def test_sqlite_fallback_surfaces_swept_at_utc(monkeypatch):
    """sweptAtUtc from SQLite fallback must appear in best_plays_for_date output."""
    sqlite_entry = {
        "pickDate": "2026-03-09",
        "playerId": 1631204,
        "playerName": "Marcus Sasser",
        "playerTeamAbbr": "DET",
        "opponentAbbr": "BKN",
        "stat": "pts",
        "line": 10.5,
        "overOdds": -110,
        "underOdds": -110,
        "recommendedSide": "over",
        "recommendedEvPct": 12.5,
        "recommendedOdds": -110,
        "probOver": 0.62,
        "probUnder": 0.38,
        "projection": 12.0,
        "settled": False,
        "source": "sqlite_fallback",
        "sweptAtUtc": "2026-03-09T19:55:00Z",
    }
    monkeypatch.setattr(bt, "_load_journal_entries", lambda: [])
    monkeypatch.setattr(bt, "_sqlite_fallback_entries", lambda target: [sqlite_entry])
    monkeypatch.setattr(bt, "_get_playing_teams_today", lambda target_date=None: {"DET", "BKN"})
    monkeypatch.setattr(bt, "_load_line_history", lambda target, phase=None: {})
    monkeypatch.setattr(bt, "_get_pulled_players", lambda target_date: set())

    result = bt.best_plays_for_date("2026-03-09", limit=5)
    assert result["success"] is True
    row = result["topOffers"][0]
    assert row["sweptAtUtc"] == "2026-03-09T19:55:00Z"


def test_legacy_swept_at_est_falls_back_correctly(monkeypatch):
    """Rows with old sweptAtEst field should still surface via the fallback chain."""
    entry = {
        "entryId": "legacy-1",
        "createdAtUtc": "2026-03-09T20:00:00Z",
        "createdAtLocal": "2026-03-09T14:00:00",
        "pickDate": "2026-03-09",
        "playerId": 1631204,
        "playerName": "Marcus Sasser",
        "playerTeamAbbr": "DET",
        "opponentAbbr": "BKN",
        "isHome": True,
        "isB2B": False,
        "stat": "pts",
        "line": 10.5,
        "overOdds": -110,
        "underOdds": -110,
        "recommendedSide": "over",
        "recommendedEvPct": 12.5,
        "recommendedOdds": -110,
        "probOver": 0.62,
        "probUnder": 0.38,
        "projection": 12.0,
        "settled": False,
        "result": None,
        # Legacy field name from the ET era
        "sweptAtEst": "2026-03-09T15:58:30 ET",
    }
    monkeypatch.setattr(bt, "_load_journal_entries", lambda: [entry])
    monkeypatch.setattr(bt, "_sqlite_fallback_entries", lambda target: [])
    monkeypatch.setattr(bt, "_get_playing_teams_today", lambda target_date=None: {"DET", "BKN"})
    monkeypatch.setattr(bt, "_load_line_history", lambda target, phase=None: {})
    monkeypatch.setattr(bt, "_get_pulled_players", lambda target_date: set())

    result = bt.best_plays_for_date("2026-03-09", limit=5)
    assert result["success"] is True
    row = result["topOffers"][0]
    # Legacy ET string must NOT pollute sweptAtUtc — goes to sweptAtFallback instead
    assert "sweptAtUtc" not in row
    assert row["sweptAtFallback"] == "2026-03-09T15:58:30 ET"


# ---------------------------------------------------------------------------
# Odds-differ dedup: same prop at different odds collapses to one entry
# ---------------------------------------------------------------------------

def test_odds_differ_dedup_keeps_latest(monkeypatch):
    """Same (date, player, opponent, home, b2b, stat, line) with different odds
    should be treated as a duplicate — only the latest entry survives."""
    journal_path = _scratch_journal_path()
    journal_path.unlink(missing_ok=True)
    monkeypatch.setattr(bt, "DATA_DIR", journal_path.parent)
    monkeypatch.setattr(bt, "JOURNAL_PATH", journal_path)

    first = {
        **_make_entry(entry_id="sweep1", created_at="2026-03-10T18:00:00Z"),
        "overOdds": -110,
        "underOdds": -110,
        "recommendedSide": "under",
    }
    second = {
        **_make_entry(entry_id="sweep2", created_at="2026-03-10T18:30:00Z"),
        "overOdds": -115,
        "underOdds": -105,
        "recommendedSide": "over",
    }

    first_result = bt._append_journal_entry(first)
    second_result = bt._append_journal_entry(second)
    entries = bt._load_journal_entries()

    assert first_result["isDuplicate"] is False
    assert second_result["isDuplicate"] is True
    assert len(entries) == 1
    # Latest entry wins — updated odds and recommendedSide preserved
    assert entries[0]["entryId"] == "sweep2"
    assert entries[0]["createdAtUtc"] == "2026-03-10T18:30:00Z"
    assert entries[0]["overOdds"] == -115
    assert entries[0]["underOdds"] == -105
    assert entries[0]["recommendedSide"] == "over"
    journal_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Player quality gate in log_prop_ev_entry
# ---------------------------------------------------------------------------

def _make_valid_prop_result(**overrides):
    """Build a minimal prop_result that passes team validation + commence_time."""
    base = {
        "success": True,
        "projection": {"projection": 25.0, "projStdev": 5.0},
        "ev": {
            "over": {"evPercent": 5.0, "edge": 0.10, "pSideNoPush": 0.55},
            "under": {"evPercent": -3.0, "edge": -0.03, "pSideNoPush": 0.45},
            "probOver": 0.55,
            "probUnder": 0.45,
        },
        "commenceTime": "2026-03-10T23:30:00Z",
    }
    base.update(overrides)
    return base


def test_log_prop_ev_rejects_insufficient_games(monkeypatch):
    journal_path = _scratch_journal_path()
    journal_path.unlink(missing_ok=True)
    monkeypatch.setattr(bt, "DATA_DIR", journal_path.parent)
    monkeypatch.setattr(bt, "JOURNAL_PATH", journal_path)
    # Bypass team validation
    monkeypatch.setattr(bt, "validate_player_team", lambda *a, **kw: ("MIN", True))
    monkeypatch.setattr(bt, "_PLAYERS_BY_ID", {1630596: "Anthony Edwards"})

    prop = _make_valid_prop_result(gamesPlayed=3)
    result = bt.log_prop_ev_entry(
        prop,
        player_id=1630596,
        player_identifier="Anthony Edwards",
        player_team_abbr="MIN",
        opponent_abbr="ORL",
        is_home=True,
        stat="pts",
        line=25.5,
        over_odds=-110,
        under_odds=-110,
    )
    assert result["success"] is False
    assert "insufficient_games:3" in result["error"]
    journal_path.unlink(missing_ok=True)


def test_log_prop_ev_rejects_low_minutes(monkeypatch):
    journal_path = _scratch_journal_path()
    journal_path.unlink(missing_ok=True)
    monkeypatch.setattr(bt, "DATA_DIR", journal_path.parent)
    monkeypatch.setattr(bt, "JOURNAL_PATH", journal_path)
    monkeypatch.setattr(bt, "validate_player_team", lambda *a, **kw: ("MIN", True))
    monkeypatch.setattr(bt, "_PLAYERS_BY_ID", {1630596: "Anthony Edwards"})

    prop = _make_valid_prop_result(
        gamesPlayed=20,
        minutesProjection={"seasonMinutes": 5.5, "minutesReasoning": []},
    )
    result = bt.log_prop_ev_entry(
        prop,
        player_id=1630596,
        player_identifier="Anthony Edwards",
        player_team_abbr="MIN",
        opponent_abbr="ORL",
        is_home=True,
        stat="pts",
        line=25.5,
        over_odds=-110,
        under_odds=-110,
    )
    assert result["success"] is False
    assert "low_minutes_player:5.5" in result["error"]
    journal_path.unlink(missing_ok=True)
