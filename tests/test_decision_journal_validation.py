from datetime import datetime, timedelta, timezone
import sqlite3

from core.nba_decision_journal import DecisionJournal


def _insert_signal_outcome(
    dj: DecisionJournal,
    *,
    signal_id: str,
    game_day,
    book: str,
    stat: str = "pts",
    result: str = "win",
    pnl_units: float = 1.0,
    clv_delta: float | None = 0.1,
):
    ts_utc = f"{game_day.isoformat()}T12:00:00Z"
    dj._conn.execute(
        """INSERT INTO signals (
            signal_id, ts_utc, signal_version, player_id, player_name,
            team_abbr, opponent_abbr, stat, line, book, over_odds, under_odds,
            projection, prob_over, prob_under, edge_over, edge_under,
            recommended_side, recommended_edge, confidence, used_real_line,
            action_taken, skip_reason, context_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            signal_id, ts_utc, "v1", 1, "Test Player",
            "BOS", "NYK", stat, 25.5, book, -110, -110,
            24.0, 0.05, 0.95, 0.10, 0.0,
            "under", 0.10, 0.65, 1, 1, None, None,
        ),
    )
    dj._conn.execute(
        """INSERT INTO outcomes (
            signal_id, game_id, settle_date, result, pnl_units,
            close_line, close_over_odds, close_under_odds, clv_delta, settled_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            signal_id, f"game-{signal_id}", game_day.isoformat(), result, pnl_units,
            25.5, -110, -110, clv_delta, ts_utc,
        ),
    )
    dj._conn.commit()


def _make_memory_journal() -> DecisionJournal:
    dj = DecisionJournal.__new__(DecisionJournal)
    dj._path = ":memory:"
    dj._conn = sqlite3.connect(":memory:", check_same_thread=False)
    dj._init_db()
    return dj


def test_gate_check_excludes_user_supplied_signals():
    dj = _make_memory_journal()
    today = datetime.now(timezone.utc).date()

    _insert_signal_outcome(
        dj, signal_id="auto-win", game_day=today, book="BetMGM",
        stat="pts", result="win", pnl_units=1.0,
    )
    _insert_signal_outcome(
        dj, signal_id="auto-loss", game_day=today, book="FanDuel",
        stat="ast", result="loss", pnl_units=-1.0,
    )
    _insert_signal_outcome(
        dj, signal_id="manual-win", game_day=today, book="user_supplied",
        stat="pts", result="win", pnl_units=1.0,
    )

    result = dj.gate_check(
        window_days=14,
        min_sample=1,
        min_roi=-1.0,
        min_positive_clv_pct=0.0,
    )

    dj.close()

    assert result["metrics"]["sample"] == 2
    assert result["metrics"]["roi"] == 0.0
    assert result["model_leans"]["sample"] == 2
    assert result["config"]["signals_excluded"] == 1
    assert result["config"]["excluded_books"] == ["user_supplied"]


def test_generate_report_treats_manual_only_days_as_gaps():
    dj = _make_memory_journal()
    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)

    _insert_signal_outcome(
        dj, signal_id="auto-win", game_day=today, book="BetMGM",
        stat="pts", result="win", pnl_units=1.0,
    )
    _insert_signal_outcome(
        dj, signal_id="manual-win", game_day=tomorrow, book="user_supplied",
        stat="pts", result="win", pnl_units=1.0,
    )

    report = dj.generate_report(
        date_from=today.isoformat(),
        date_to=(tomorrow + timedelta(days=1)).isoformat(),
    )

    dj.close()

    assert report["qualifying_count"] == 1
    assert report["settled_count"] == 1
    assert report["exclusions"]["signalsExcluded"] == 1
    assert report["coverage"]["daysWithSignals"] == 1
    assert report["coverage"]["gapDates"] == [tomorrow.isoformat()]
