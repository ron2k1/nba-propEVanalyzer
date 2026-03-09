import pickle
import uuid
from pathlib import Path

from scripts import extend_local_index as ext


def test_upsert_game_entry_is_idempotent_and_updates():
    games_by_date = {}
    base = {
        "gameId": "0022500809",
        "homeTeamId": 1610612747,
        "awayTeamId": 1610612746,
        "homeAbbr": "LAL",
        "awayAbbr": "LAC",
        "homePts": 125,
        "awayPts": 122,
        "gameDate": "2026-02-20",
    }

    assert ext._upsert_game_entry(games_by_date, dict(base)) == "added"
    assert ext._upsert_game_entry(games_by_date, dict(base)) == "unchanged"

    updated = dict(base)
    updated["homePts"] = 126
    assert ext._upsert_game_entry(games_by_date, updated) == "updated"
    assert games_by_date["2026-02-20"][0]["homePts"] == 126


def test_upsert_boxscore_and_gamelog_rows_are_idempotent():
    boxscore_by_game = {}
    gamelogs_by_player = {}

    box_row = {
        "PLAYER_ID": 1627750,
        "TEAM_ID": 1610612747,
        "MIN": "PT34M12.00S",
        "PTS": 22,
        "REB": 5,
        "AST": 7,
        "STL": 1,
        "BLK": 0,
        "TOV": 2,
        "FG3M": 3,
    }
    assert ext._upsert_boxscore_row(boxscore_by_game, "0022500809", dict(box_row)) == "added"
    assert ext._upsert_boxscore_row(boxscore_by_game, "0022500809", dict(box_row)) == "unchanged"

    box_row_updated = dict(box_row)
    box_row_updated["PTS"] = 24
    assert ext._upsert_boxscore_row(boxscore_by_game, "0022500809", box_row_updated) == "updated"
    assert boxscore_by_game["0022500809"][0]["PTS"] == 24

    log_row = {
        "gameDate": "Feb 20, 2026",
        "gameId": "0022500809",
        "matchup": "LAL vs. LAC",
        "opponent": "LAC",
        "isHome": True,
        "wl": "W",
        "min": 34.2,
        "pts": 24,
        "reb": 5,
        "ast": 7,
        "stl": 1,
        "blk": 0,
        "tov": 2,
        "fg3m": 3,
        "fg3a": 8,
        "fgm": 9,
        "fga": 18,
        "ftm": 3,
        "fta": 4,
        "fgPct": 50.0,
        "fg3Pct": 37.5,
        "ftPct": 75.0,
        "plusMinus": 8,
        "pra": 36,
        "pr": 29,
        "pa": 31,
        "ra": 12,
        "stocksBlkStl": 1,
        "_date_str": "2026-02-20",
        "_team_id": 1610612747,
        "_team_abbr": "LAL",
        "season": "2025-26",
    }
    assert ext._upsert_gamelog_row(gamelogs_by_player, 1627750, dict(log_row)) == "added"
    assert ext._upsert_gamelog_row(gamelogs_by_player, 1627750, dict(log_row)) == "unchanged"

    log_row_updated = dict(log_row)
    log_row_updated["plusMinus"] = 10
    assert ext._upsert_gamelog_row(gamelogs_by_player, 1627750, log_row_updated) == "updated"
    assert gamelogs_by_player[1627750][0]["plusMinus"] == 10


def test_atomic_pickle_dump_replaces_target_contents():
    path = Path.cwd() / f".tmp_extend_index_{uuid.uuid4().hex}.pkl"
    try:
        ext._atomic_pickle_dump(str(path), {"max_date": "2026-03-05", "rows": [1, 2, 3]})
        with open(path, "rb") as fh:
            payload = pickle.load(fh)
        assert payload["max_date"] == "2026-03-05"
        assert payload["rows"] == [1, 2, 3]
    finally:
        path.unlink(missing_ok=True)
