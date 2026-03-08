import json
from pathlib import Path

from core import nba_bet_tracking as bt
from core import nba_model_ml_training as ml


def _scratch_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "data" / name


def test_build_outcome_feature_row_handles_historical_schema():
    row = {
        "stat": "pts",
        "side": "under",
        "projection": 18.0,
        "line": 21.5,
        "prob_over": 0.22,
        "edge": 0.14,
        "odds": -118,
        "bin": 2,
        "used_real_line": 1,
        "n_games": 18,
        "shrink_weight": 0.37,
    }

    feature_row = ml.build_outcome_feature_row(row)

    assert feature_row is not None
    assert feature_row["lineDiff"] == -3.5
    assert feature_row["probChosenSide"] > 0.75
    assert feature_row["sideIsOver"] == 0.0
    assert feature_row["edgeUnit"] == 0.14
    assert feature_row["statIs_pts"] == 1.0
    assert feature_row["statIs_other"] == 0.0
    assert feature_row["nGames"] == 18.0
    assert feature_row["shrinkWeight"] == 0.37


def test_train_outcome_ml_from_file_and_predict():
    data_path = _scratch_path("_test_outcome_rows.jsonl")
    out_path = _scratch_path("_test_outcome_model.pkl")
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)
    rows = []
    for idx in range(700):
        line = 10.0 + float(idx % 4)
        projection = line + (2.5 if idx % 2 == 0 else -2.5)
        side = "over" if idx % 4 < 2 else "under"
        prob_over = 0.82 if projection > line else 0.18
        win = (side == "over" and projection > line) or (side == "under" and projection < line)
        stat = "reb" if idx % 11 == 0 else ("ast" if idx % 3 == 0 else "pts")
        rows.append(
            {
                "date": f"2026-01-{(idx % 28) + 1:02d}",
                "stat": stat,
                "side": side,
                "projection": projection,
                "line": line,
                "prob_over": prob_over,
                "edge": abs(projection - line) / 10.0,
                "odds": -110 if side == "over" else -118,
                "bin": max(0, min(9, int(prob_over * 10))),
                "used_real_line": 1,
                "n_games": 12 + (idx % 9),
                "shrink_weight": round(0.2 + ((idx % 10) * 0.05), 3),
                "outcome": "win" if win else "loss",
            }
        )
    data_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = ml.train_outcome_ml_from_file(
        str(data_path),
        holdout_frac=0.2,
        min_holdout=40,
        model_type="logistic",
        output_model_path=str(out_path),
        filter_stats=["pts", "ast"],
    )

    assert result["success"] is True
    assert out_path.exists()
    assert result["filterStats"] == ["ast", "pts"]
    assert result["metrics"]["holdout"]["accuracy"] >= 0.7

    loaded = ml.load_outcome_ml_bundle(str(out_path))
    assert loaded["success"] is True
    assert loaded["bundle"]["filterStats"] == ["ast", "pts"]
    pred = ml.predict_outcome_ml(
        loaded["bundle"],
        {
            "stat": "pts",
            "recommendedSide": "over",
            "projection": 16.0,
            "line": 12.5,
            "probOver": 0.84,
            "recommendedEvPct": 18.0,
            "recommendedOdds": -110,
            "usedRealLine": True,
            "nGames": 16,
            "shrinkWeight": 0.4,
        },
    )
    assert pred is not None
    assert 0.0 <= pred <= 1.0
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)


def test_score_rows_with_outcome_ml_skips_stats_outside_filter():
    data_path = _scratch_path("_test_outcome_rows_filter.jsonl")
    out_path = _scratch_path("_test_outcome_filter_model.pkl")
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)
    rows = []
    for idx in range(620):
        stat = "ast" if idx % 2 == 0 else "pts"
        line = 8.0 + float(idx % 3)
        projection = line + (3.0 if idx % 4 < 2 else -3.0)
        side = "over" if idx % 2 == 0 else "under"
        prob_over = 0.84 if projection > line else 0.16
        win = (side == "over" and projection > line) or (side == "under" and projection < line)
        rows.append(
            {
                "date": f"2026-02-{(idx % 28) + 1:02d}",
                "stat": stat,
                "side": side,
                "projection": projection,
                "line": line,
                "prob_over": prob_over,
                "edge": 0.18,
                "odds": -110,
                "bin": max(0, min(9, int(prob_over * 10))),
                "used_real_line": 1,
                "n_games": 20,
                "shrink_weight": 0.45,
                "outcome": "win" if win else "loss",
            }
        )
    data_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = ml.train_outcome_ml_from_file(
        str(data_path),
        holdout_frac=0.2,
        min_holdout=50,
        model_type="logistic",
        output_model_path=str(out_path),
        filter_stats=["pts", "ast"],
    )
    assert result["success"] is True

    scored = ml.score_rows_with_outcome_ml(
        [
            {
                "stat": "pts",
                "recommendedSide": "over",
                "projection": 16.0,
                "line": 12.0,
                "probOver": 0.82,
                "recommendedOdds": -110,
                "usedRealLine": True,
                "nGames": 18,
                "shrinkWeight": 0.42,
            },
            {
                "stat": "reb",
                "recommendedSide": "over",
                "projection": 11.0,
                "line": 8.5,
                "probOver": 0.77,
                "recommendedOdds": -110,
                "usedRealLine": True,
                "nGames": 18,
                "shrinkWeight": 0.42,
            },
        ],
        model_path=str(out_path),
    )

    assert scored["success"] is True
    assert scored["filterStats"] == ["ast", "pts"]
    assert "outcomeModelWinProb" in scored["rows"][0]
    assert "outcomeModelWinProb" not in scored["rows"][1]
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)


def test_best_plays_for_date_adds_outcome_model_scores(monkeypatch):
    base_entry = {
        "entryId": "signal-1",
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

    monkeypatch.setattr(bt, "_load_journal_entries", lambda: [base_entry])
    monkeypatch.setattr(bt, "_sqlite_fallback_entries", lambda target: [])
    monkeypatch.setattr(bt, "_get_playing_teams_today", lambda target_date=None: {"DET", "BKN"})
    monkeypatch.setattr(bt, "_load_line_history", lambda target: {})

    def _fake_score(rows, model_path=None):
        enriched = []
        for row in rows:
            item = dict(row)
            item["outcomeModelWinProb"] = 0.73
            item["outcomeModelEvPct"] = 12.4
            enriched.append(item)
        return {
            "success": True,
            "loaded": True,
            "modelPath": str(Path("models/fake_outcome.pkl")),
            "modelType": "logistic",
            "rows": enriched,
        }

    monkeypatch.setattr(ml, "score_rows_with_outcome_ml", _fake_score)

    result = bt.best_plays_for_date("2026-03-07", limit=10)

    assert result["success"] is True
    assert result["outcomeModel"]["loaded"] is True
    assert result["topOffers"][0]["outcomeModelWinProb"] == 0.73
    assert result["policyQualified"][0]["outcomeModelEvPct"] == 12.4
