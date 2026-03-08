from core import nba_prop_engine


def test_compute_prop_ev_skips_market_refresh_when_disabled(monkeypatch):
    monkeypatch.setattr(
        nba_prop_engine,
        "compute_projection",
        lambda **kwargs: {
            "success": True,
            "projections": {
                "pts": {
                    "projection": 24.5,
                    "projStdev": 4.0,
                }
            },
            "matchupHistory": None,
            "opponentDefense": None,
            "position": "G",
            "gamesPlayed": 10,
            "modelVariant": "full",
            "minutesProjection": None,
            "usageAdjustment": None,
        },
    )
    monkeypatch.setattr(
        nba_prop_engine,
        "get_nba_player_prop_offers",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("market refresh should be skipped")),
    )
    monkeypatch.setattr(nba_prop_engine, "_ALL_PLAYERS_BY_ID", {1: "Test Player"})

    result = nba_prop_engine.compute_prop_ev(
        player_id=1,
        opponent_abbr="NYK",
        is_home=True,
        stat="pts",
        line=22.5,
        over_odds=-110,
        under_odds=-105,
        player_team_abbr="BOS",
        refresh_market_offers=False,
    )

    assert result["success"] is True
    assert result["bestOverOdds"] == -110
    assert result["bestUnderOdds"] == -105
    assert result["lineShopping"] == {
        "skipped": True,
        "reason": "market_refresh_disabled",
    }
