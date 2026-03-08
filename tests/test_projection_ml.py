import json
from pathlib import Path

from core import nba_model_ml_training as ml


def _scratch_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "data" / name


def _projection_rows():
    rows = []
    for idx in range(520):
        stat = "pts" if idx % 2 == 0 else "ast"
        projection = 18.0 + float(idx % 9) + (1.5 if stat == "pts" else 0.0)
        line = projection - (1.2 if idx % 3 == 0 else -0.8)
        line_diff = projection - line
        prob_over = 0.68 if line_diff > 0 else 0.32
        prob_under = 1.0 - prob_over
        is_home = 1 if idx % 2 == 0 else 0
        actual = projection + (0.45 if is_home else -0.35) - (0.12 * (idx % 4))
        rows.append(
            {
                "pickDate": f"2026-01-{(idx % 28) + 1:02d}",
                "stat": stat,
                "projection": projection,
                "projStdev": 2.4 + (idx % 4) * 0.1,
                "lineDiff": line_diff,
                "probOver": prob_over,
                "probUnder": prob_under,
                "probPush": 0.02,
                "impliedOverProb": 0.52,
                "impliedUnderProb": 0.52,
                "evOverPct": (prob_over - 0.52) * 100.0,
                "evUnderPct": (prob_under - 0.52) * 100.0,
                "bestEvPct": max((prob_over - 0.52) * 100.0, (prob_under - 0.52) * 100.0),
                "bestIsOver": 1 if prob_over >= prob_under else 0,
                "isHome": is_home,
                "isB2B": 1 if idx % 5 == 0 else 0,
                "actual": actual,
            }
        )
    return rows


def test_train_projection_ml_from_file():
    data_path = _scratch_path("_test_projection_rows.jsonl")
    out_path = _scratch_path("_test_projection_model.pkl")
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)
    rows = _projection_rows()
    data_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = ml.train_projection_ml_from_file(
        str(data_path),
        holdout_frac=0.2,
        min_holdout=50,
        model_type="linear",
        output_model_path=str(out_path),
    )

    assert result["success"] is True
    assert out_path.exists()
    assert result["metrics"]["holdout"]["count"] >= 50
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)


def test_train_projection_ml_per_stat_from_file():
    data_path = _scratch_path("_test_projection_per_stat_rows.jsonl")
    out_path = _scratch_path("_test_projection_per_stat_model.pkl")
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)
    rows = _projection_rows()
    data_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = ml.train_projection_ml_per_stat_from_file(
        str(data_path),
        stat_key="stat",
        target_key="actual",
        holdout_frac=0.2,
        min_holdout=25,
        min_train=100,
        model_type="linear",
        output_model_path=str(out_path),
    )

    assert result["success"] is True
    assert out_path.exists()
    assert set(result["statsTrained"]) == {"ast", "pts"}
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)


def test_train_quantile_projection_from_file():
    data_path = _scratch_path("_test_quantile_rows.jsonl")
    out_path = _scratch_path("_test_quantile_model.pkl")
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)
    rows = _projection_rows()
    data_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = ml.train_quantile_projection_from_file(
        str(data_path),
        quantiles=(0.1, 0.5, 0.9),
        output_model_path=str(out_path),
    )
    loaded = ml.load_projection_ml_bundle(str(out_path))
    quantile_preds = ml.predict_quantile_projection(
        loaded["bundle"],
        {
            "projection": 21.0,
            "projStdev": 2.6,
            "lineDiff": 1.2,
            "probOver": 0.68,
            "probUnder": 0.32,
            "probPush": 0.02,
            "impliedOverProb": 0.52,
            "impliedUnderProb": 0.52,
            "evOverPct": 16.0,
            "evUnderPct": -20.0,
            "bestEvPct": 16.0,
            "bestIsOver": 1,
            "isHome": 1,
            "isB2B": 0,
        },
    )

    assert result["success"] is True
    assert out_path.exists()
    assert loaded["success"] is True
    assert sorted(quantile_preds.keys()) == [0.1, 0.5, 0.9]
    prob_over = ml.prob_over_from_quantiles(20.5, quantile_preds)
    assert prob_over is not None
    assert 0.0 <= prob_over <= 1.0
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)
