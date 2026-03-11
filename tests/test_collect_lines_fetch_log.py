"""Test that collect_lines fetch log includes matchup identifiers."""

from datetime import datetime, timezone, timedelta

from core import nba_data_collection as dc


def _make_commence_time():
    """Return a commence_time that passes _event_is_today() filter."""
    # 30 minutes from now — safely within the 4h window and today's date
    dt = datetime.now(timezone.utc) + timedelta(minutes=30)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_fetch_log_entries_contain_matchup_field(monkeypatch):
    """Each entry in fetchLog must have a human-readable matchup like 'BKN @ DET'."""

    commence = _make_commence_time()

    # Mock the Odds API event discovery
    monkeypatch.setattr(
        dc,
        "_odds_api_get",
        lambda path, params=None, timeout=30: {
            "success": True,
            "data": [
                {
                    "id": "event-abc",
                    "home_team": "Detroit Pistons",
                    "away_team": "Brooklyn Nets",
                    "commence_time": commence,
                },
            ],
        },
    )

    # Mock the per-stat prop fetch
    monkeypatch.setattr(
        dc,
        "get_event_player_props_bulk",
        lambda **kwargs: {
            "success": True,
            "snapshots": [
                {
                    "player_name": "Marcus Sasser",
                    "line": 10.5,
                    "over_odds": -110,
                    "under_odds": -110,
                    "book": "betmgm",
                },
            ],
        },
    )

    # Mock player-team map
    monkeypatch.setattr(dc, "get_player_team_map", lambda max_age_sec=3600: {
        "marcus sasser": "DET",
    })

    result = dc.get_todays_event_props_bulk(
        bookmakers="betmgm",
        stats=["pts"],
    )

    assert result["success"] is True
    assert "fetchLog" in result
    log = result["fetchLog"]
    assert len(log) == 1

    entry = log[0]
    assert "matchup" in entry, "fetchLog entry must contain 'matchup' field"
    assert entry["matchup"] == "BKN @ DET"
    assert entry["stat"] == "pts"
    assert entry["success"] is True
    assert entry["snapshotCount"] == 1
    assert "fetchedAt" in entry
    assert entry["fetchedAt"].endswith("Z")
