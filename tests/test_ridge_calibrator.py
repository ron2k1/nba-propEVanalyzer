import json
from pathlib import Path

from core import nba_model_ml_training as ml


def _scratch_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "data" / name


def test_train_ridge_calibrator_predicts():
    rows = []
    for idx in range(80):
        projection = 12.0 + idx * 0.1
        line_diff = (-1.0 if idx % 2 else 1.5)
        actual = projection + (0.4 * line_diff)
        rows.append(
            {
                "projection": projection,
                "lineDiff": line_diff,
                "actual": actual,
            }
        )

    result = ml.train_ridge_calibrator(
        rows,
        feature_keys=["projection", "lineDiff"],
        target_key="actual",
        ridge_alpha=0.25,
    )

    assert result["success"] is True
    pred = ml.predict_ridge_calibrator(
        result["model"],
        {"projection": 18.5, "lineDiff": 1.1},
    )
    assert pred is not None
    assert pred > 0


def test_train_ridge_calibrator_from_file():
    data_path = _scratch_path("_test_ridge_rows.jsonl")
    out_path = _scratch_path("_test_ridge_model.json")
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)

    rows = []
    for idx in range(90):
        projection = 9.0 + idx * 0.12
        line_diff = 0.8 if idx % 3 == 0 else -0.6
        rows.append(
            {
                "projection": projection,
                "lineDiff": line_diff,
                "actual": projection + (0.35 * line_diff),
            }
        )
    data_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = ml.train_ridge_calibrator_from_file(
        str(data_path),
        target_key="actual",
        feature_keys=["projection", "lineDiff"],
        ridge_alpha=0.5,
        output_model_path=str(out_path),
    )

    assert result["success"] is True
    assert out_path.exists()
    assert result["metrics"]["trainRows"] == 90
    data_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)
