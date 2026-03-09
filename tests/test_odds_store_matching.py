import uuid
from pathlib import Path

from core.nba_odds_store import OddsStore, _resolve_player_name_candidate


def _make_store():
    db_path = Path.cwd() / f".tmp_odds_store_{uuid.uuid4().hex}.sqlite"
    store = OddsStore(str(db_path))
    return db_path, store


def test_resolve_player_name_candidate_handles_aliases_and_initials():
    candidates = [
        "A.J. Green",
        "Nicolas Claxton",
        "Moritz Wagner",
    ]

    assert _resolve_player_name_candidate("AJ Green", candidates) == "A.J. Green"
    assert _resolve_player_name_candidate("Nic Claxton", candidates) == "Nicolas Claxton"
    assert _resolve_player_name_candidate("Moe Wagner", candidates) == "Moritz Wagner"


def test_resolve_player_name_candidate_keeps_ambiguous_last_name_unmatched():
    candidates = [
        "Cameron Johnson",
        "Keldon Johnson",
    ]

    assert _resolve_player_name_candidate("Johnson", candidates) is None


def test_get_closing_line_matches_normalized_name():
    db_path, store = _make_store()
    try:
        store.upsert_closing_lines(
            [
                {
                    "event_id": "evt1",
                    "book": "betmgm",
                    "market": "player_points",
                    "player_name": "A.J. Green",
                    "close_ts_utc": "2026-02-20T00:10:00Z",
                    "close_line": 9.5,
                    "close_over_odds": -110,
                    "close_under_odds": -110,
                    "commence_time": "2026-02-20T01:00:00Z",
                }
            ]
        )

        row = store.get_closing_line("evt1", "player_points", "AJ Green")
        assert row is not None
        assert row["close_line"] == 9.5
    finally:
        store.close()
        db_path.unlink(missing_ok=True)


def test_get_closing_line_by_player_date_matches_alias_name():
    db_path, store = _make_store()
    try:
        store.upsert_closing_lines(
            [
                {
                    "event_id": "evt2",
                    "book": "betmgm",
                    "market": "player_points",
                    "player_name": "Moritz Wagner",
                    "close_ts_utc": "2026-02-21T00:10:00Z",
                    "close_line": 12.5,
                    "close_over_odds": -105,
                    "close_under_odds": -115,
                    "commence_time": "2026-02-21T01:00:00Z",
                }
            ]
        )

        row = store.get_closing_line_by_player_date("Moe Wagner", "player_points", "2026-02-20")
        assert row is not None
        assert row["close_line"] == 12.5
    finally:
        store.close()
        db_path.unlink(missing_ok=True)


def test_get_opening_line_matches_alias_name():
    db_path, store = _make_store()
    try:
        store.upsert_snapshots(
            [
                {
                    "ts_utc": "2026-02-20T19:00:00Z",
                    "sport": "basketball_nba",
                    "event_id": "evt3",
                    "book": "betmgm",
                    "market": "player_rebounds",
                    "player_name": "Nicolas Claxton",
                    "side": "over",
                    "line": 10.5,
                    "odds": -120,
                    "home_team": "Brooklyn Nets",
                    "away_team": "Boston Celtics",
                    "commence_time": "2026-02-21T00:00:00Z",
                    "source": "test",
                },
                {
                    "ts_utc": "2026-02-20T19:00:00Z",
                    "sport": "basketball_nba",
                    "event_id": "evt3",
                    "book": "betmgm",
                    "market": "player_rebounds",
                    "player_name": "Nicolas Claxton",
                    "side": "under",
                    "line": 10.5,
                    "odds": -110,
                    "home_team": "Brooklyn Nets",
                    "away_team": "Boston Celtics",
                    "commence_time": "2026-02-21T00:00:00Z",
                    "source": "test",
                },
            ]
        )

        row = store.get_opening_line("evt3", "player_rebounds", "Nic Claxton")
        assert row is not None
        assert row["open_line"] == 10.5
        assert row["open_over_odds"] == -120
        assert row["open_under_odds"] == -110
    finally:
        store.close()
        db_path.unlink(missing_ok=True)


def test_get_line_movement_matches_alias_name():
    db_path, store = _make_store()
    try:
        store.upsert_snapshots(
            [
                {
                    "ts_utc": "2026-02-20T18:00:00Z",
                    "sport": "basketball_nba",
                    "event_id": "evt4",
                    "book": "betmgm",
                    "market": "player_rebounds",
                    "player_name": "Nicolas Claxton",
                    "side": "over",
                    "line": 9.5,
                    "odds": -105,
                    "home_team": "Brooklyn Nets",
                    "away_team": "Boston Celtics",
                    "commence_time": "2026-02-21T00:00:00Z",
                    "source": "test",
                },
                {
                    "ts_utc": "2026-02-20T18:00:00Z",
                    "sport": "basketball_nba",
                    "event_id": "evt4",
                    "book": "betmgm",
                    "market": "player_rebounds",
                    "player_name": "Nicolas Claxton",
                    "side": "under",
                    "line": 9.5,
                    "odds": -115,
                    "home_team": "Brooklyn Nets",
                    "away_team": "Boston Celtics",
                    "commence_time": "2026-02-21T00:00:00Z",
                    "source": "test",
                },
                {
                    "ts_utc": "2026-02-20T19:30:00Z",
                    "sport": "basketball_nba",
                    "event_id": "evt4",
                    "book": "betmgm",
                    "market": "player_rebounds",
                    "player_name": "Nicolas Claxton",
                    "side": "over",
                    "line": 10.5,
                    "odds": -120,
                    "home_team": "Brooklyn Nets",
                    "away_team": "Boston Celtics",
                    "commence_time": "2026-02-21T00:00:00Z",
                    "source": "test",
                },
                {
                    "ts_utc": "2026-02-20T19:30:00Z",
                    "sport": "basketball_nba",
                    "event_id": "evt4",
                    "book": "betmgm",
                    "market": "player_rebounds",
                    "player_name": "Nicolas Claxton",
                    "side": "under",
                    "line": 10.5,
                    "odds": -110,
                    "home_team": "Brooklyn Nets",
                    "away_team": "Boston Celtics",
                    "commence_time": "2026-02-21T00:00:00Z",
                    "source": "test",
                },
            ]
        )

        rows = store.get_line_movement("evt4", "player_rebounds", "Nic Claxton")
        assert len(rows) == 2
        assert rows[0]["line"] == 9.5
        assert rows[1]["line"] == 10.5
        assert rows[0]["minutes_to_tip"] == 360.0
        assert rows[1]["minutes_to_tip"] == 270.0
    finally:
        store.close()
        db_path.unlink(missing_ok=True)


def test_get_line_movement_by_date_matches_alias_name():
    db_path, store = _make_store()
    try:
        store.upsert_snapshots(
            [
                {
                    "ts_utc": "2026-02-20T18:45:00Z",
                    "sport": "basketball_nba",
                    "event_id": "evt5",
                    "book": "betmgm",
                    "market": "player_points",
                    "player_name": "Moritz Wagner",
                    "side": "over",
                    "line": 11.5,
                    "odds": -102,
                    "home_team": "Orlando Magic",
                    "away_team": "Cleveland Cavaliers",
                    "commence_time": "2026-02-21T00:15:00Z",
                    "source": "test",
                },
                {
                    "ts_utc": "2026-02-20T18:45:00Z",
                    "sport": "basketball_nba",
                    "event_id": "evt5",
                    "book": "betmgm",
                    "market": "player_points",
                    "player_name": "Moritz Wagner",
                    "side": "under",
                    "line": 11.5,
                    "odds": -118,
                    "home_team": "Orlando Magic",
                    "away_team": "Cleveland Cavaliers",
                    "commence_time": "2026-02-21T00:15:00Z",
                    "source": "test",
                },
            ]
        )

        rows = store.get_line_movement_by_date("Moe Wagner", "player_points", "2026-02-20")
        assert len(rows) == 1
        assert rows[0]["line"] == 11.5
        assert rows[0]["over_odds"] == -102
        assert rows[0]["under_odds"] == -118
    finally:
        store.close()
        db_path.unlink(missing_ok=True)
