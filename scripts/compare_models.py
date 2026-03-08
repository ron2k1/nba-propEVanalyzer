#!/usr/bin/env python3
"""Summarize saved model artifacts into a markdown comparison table."""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "reports" / "model-comparison.md"


def _safe_metric(metrics: dict, *keys):
    current = metrics or {}
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _format_value(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value)
    return str(value)


def _load_model_payload(path: Path):
    with path.open("rb") as handle:
        payload = pickle.load(handle)
    metrics = payload.get("metrics") or {}
    holdout = metrics.get("holdout") or {}
    task = payload.get("task") or ("quantile_projection" if payload.get("quantile") else "projection_regression")
    return {
        "path": str(path),
        "name": path.name,
        "task": task,
        "modelType": payload.get("modelType", "-"),
        "filterStats": payload.get("filterStats"),
        "classWeightBalance": payload.get("classWeightBalance"),
        "holdoutCount": _safe_metric(metrics, "nRowsHoldoutUsed") or holdout.get("count"),
        "accuracy": holdout.get("accuracy"),
        "rocAuc": holdout.get("rocAuc"),
        "brier": holdout.get("brier"),
        "precision": holdout.get("precision"),
        "recall": holdout.get("recall"),
        "mae": holdout.get("mae"),
        "rmse": holdout.get("rmse"),
        "r2": holdout.get("r2"),
    }


def _markdown_table(rows):
    headers = [
        "Name",
        "Task",
        "Model",
        "Filter Stats",
        "Balanced",
        "Holdout N",
        "Accuracy",
        "ROC AUC",
        "Brier",
        "Precision",
        "Recall",
        "MAE",
        "RMSE",
        "R2",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join(
                [
                    _format_value(row["name"]),
                    _format_value(row["task"]),
                    _format_value(row["modelType"]),
                    _format_value(row["filterStats"]),
                    _format_value(row["classWeightBalance"]),
                    _format_value(row["holdoutCount"]),
                    _format_value(row["accuracy"]),
                    _format_value(row["rocAuc"]),
                    _format_value(row["brier"]),
                    _format_value(row["precision"]),
                    _format_value(row["recall"]),
                    _format_value(row["mae"]),
                    _format_value(row["rmse"]),
                    _format_value(row["r2"]),
                ]
            ) + " |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare saved model artifacts.")
    parser.add_argument("model_paths", nargs="*", help="Specific .pkl model paths. Defaults to models/*.pkl.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    if args.model_paths:
        paths = [Path(path).expanduser().resolve() for path in args.model_paths]
    else:
        paths = sorted((ROOT / "models").glob("*.pkl"))

    rows = []
    errors = []
    for path in paths:
        if not path.exists():
            errors.append({"path": str(path), "error": "file_not_found"})
            continue
        try:
            rows.append(_load_model_payload(path))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})

    rows.sort(
        key=lambda row: (
            0 if row["task"] == "outcome_classifier" else 1,
            -(row["rocAuc"] or -1.0),
            -(row["accuracy"] or -1.0),
            row["name"],
        )
    )

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = "# Model Comparison\n\n" + _markdown_table(rows)
    if errors:
        markdown += "\n## Load Errors\n\n```json\n" + json.dumps(errors, indent=2) + "\n```\n"
    output_path.write_text(markdown, encoding="utf-8")

    print(
        json.dumps(
            {
                "success": True,
                "outputPath": str(output_path),
                "modelCount": len(rows),
                "errorCount": len(errors),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
