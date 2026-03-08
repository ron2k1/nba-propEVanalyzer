#!/usr/bin/env python3
"""Training and model promotion CLI commands."""

import os

from core.nba_model_training import (
    promote_projection_ml_model,
    train_outcome_ml_from_file,
    train_projection_ml_from_file,
    train_projection_ml_per_stat_from_file,
    train_quantile_projection_from_file,
    train_ridge_calibrator_from_file,
)


def handle_ml_command(command, argv):
    if command == "train_projection_ml":
        if len(argv) < 3:
            return {
                "error": (
                    "Usage: train_projection_ml <data_path> [target_key] [feature_keys_csv|auto] "
                    "[holdout_frac] [min_holdout] [model_type] [date_key] [output_model_path]\n"
                    "model_type: gradient_boosting|hist_gbr|xgboost|lightgbm|random_forest|linear|tabpfn"
                )
            }
        data_path = argv[2]
        target_key = argv[3] if len(argv) > 3 else "actual"
        feature_arg = argv[4] if len(argv) > 4 else "auto"
        holdout_frac = float(argv[5]) if len(argv) > 5 else 0.2
        min_holdout = int(argv[6]) if len(argv) > 6 else 50
        model_type = argv[7] if len(argv) > 7 else "gradient_boosting"
        date_key = argv[8] if len(argv) > 8 else "pickDate"
        output_model_path = argv[9] if len(argv) > 9 else None

        feature_keys = None if feature_arg.lower() == "auto" else [
            k.strip() for k in feature_arg.split(",") if k.strip()
        ]

        return train_projection_ml_from_file(
            data_path=data_path,
            target_key=target_key,
            feature_keys=feature_keys,
            holdout_frac=holdout_frac,
            min_holdout=min_holdout,
            model_type=model_type,
            date_key=date_key,
            output_model_path=output_model_path,
        )

    if command == "train_outcome_ml":
        if len(argv) < 3:
            return {
                "error": (
                    "Usage: train_outcome_ml <data_path> [feature_keys_csv|auto] "
                    "[holdout_frac] [min_holdout] [model_type] [date_key] [output_model_path]\n"
                    "model_type: gradient_boosting|hist_gbc|xgboost|lightgbm|random_forest|logistic|tabpfn"
                )
            }
        data_path = argv[2]
        feature_arg = argv[3] if len(argv) > 3 else "auto"
        holdout_frac = float(argv[4]) if len(argv) > 4 else 0.2
        min_holdout = int(argv[5]) if len(argv) > 5 else 250
        model_type = argv[6] if len(argv) > 6 else "gradient_boosting"
        date_key = argv[7] if len(argv) > 7 else "date"
        output_model_path = argv[8] if len(argv) > 8 else None

        feature_keys = None if feature_arg.lower() == "auto" else [
            k.strip() for k in feature_arg.split(",") if k.strip()
        ]

        return train_outcome_ml_from_file(
            data_path=data_path,
            feature_keys=feature_keys,
            holdout_frac=holdout_frac,
            min_holdout=min_holdout,
            model_type=model_type,
            date_key=date_key,
            output_model_path=output_model_path,
        )

    if command == "train_projection_ml_per_stat":
        if len(argv) < 3:
            return {
                "error": (
                    "Usage: train_projection_ml_per_stat <data_path> [stat_key] [target_key] "
                    "[holdout_frac] [min_holdout] [model_type] [output_model_path]\n"
                    "Rows must have stat_key column (e.g. 'stat' with values pts, reb, ast)."
                )
            }
        data_path = argv[2]
        stat_key = argv[3] if len(argv) > 3 else "stat"
        target_key = argv[4] if len(argv) > 4 else "actual"
        holdout_frac = float(argv[5]) if len(argv) > 5 else 0.2
        min_holdout = int(argv[6]) if len(argv) > 6 else 25
        model_type = argv[7] if len(argv) > 7 else "gradient_boosting"
        output_model_path = argv[8] if len(argv) > 8 else None
        return train_projection_ml_per_stat_from_file(
            data_path=data_path,
            stat_key=stat_key,
            target_key=target_key,
            holdout_frac=holdout_frac,
            min_holdout=min_holdout,
            model_type=model_type,
            output_model_path=output_model_path,
        )

    if command == "train_quantile_projection":
        if len(argv) < 3:
            return {
                "error": (
                    "Usage: train_quantile_projection <data_path> [quantiles_csv] [output_model_path]\n"
                    "quantiles_csv default: 0.1,0.25,0.5,0.75,0.9"
                )
            }
        data_path = argv[2]
        quantiles_arg = argv[3] if len(argv) > 3 else "0.1,0.25,0.5,0.75,0.9"
        output_model_path = argv[4] if len(argv) > 4 else None
        quantiles = tuple(float(q.strip()) for q in quantiles_arg.split(",") if q.strip())
        if not quantiles:
            quantiles = (0.1, 0.25, 0.5, 0.75, 0.9)
        return train_quantile_projection_from_file(
            data_path=data_path,
            quantiles=quantiles,
            output_model_path=output_model_path,
        )

    if command == "promote_projection_ml":
        if len(argv) < 3:
            return {
                "error": (
                    "Usage: promote_projection_ml <candidate_model_path> [production_model_path] "
                    "[min_rmse_improve_pct] [min_mae_improve_pct] [force:0|1]"
                )
            }
        candidate_model_path = argv[2]
        production_model_path = argv[3] if len(argv) > 3 else None
        min_rmse_improve_pct = float(argv[4]) if len(argv) > 4 else 1.0
        min_mae_improve_pct = float(argv[5]) if len(argv) > 5 else 1.0
        force = (argv[6] == "1") if len(argv) > 6 else False

        kwargs = {}
        if production_model_path:
            kwargs["production_model_path"] = production_model_path

        return promote_projection_ml_model(
            candidate_model_path=candidate_model_path,
            min_rmse_improve_pct=min_rmse_improve_pct,
            min_mae_improve_pct=min_mae_improve_pct,
            force=force,
            **kwargs,
        )

    if command == "train_model":
        if len(argv) < 3:
            return {
                "error": (
                    "Usage: train_model <data_path> [target_key] [feature_keys_csv|auto] "
                    "[ridge_alpha] [output_model_path]"
                )
            }
        data_path = argv[2]
        target_key = argv[3] if len(argv) > 3 else "actual"
        features_arg = argv[4] if len(argv) > 4 else "auto"
        ridge_alpha = float(argv[5]) if len(argv) > 5 else 0.5

        if len(argv) > 6:
            output_model_path = argv[6]
        else:
            base = os.path.splitext(data_path)[0]
            output_model_path = base + "_ridge_model.json"

        if features_arg.lower() == "auto":
            feature_keys = None
        else:
            feature_keys = [k.strip() for k in features_arg.split(",") if k.strip()]

        result = train_ridge_calibrator_from_file(
            data_path=data_path,
            target_key=target_key,
            feature_keys=feature_keys,
            ridge_alpha=ridge_alpha,
            output_model_path=output_model_path,
        )

        if result.get("success"):
            model = result.get("model") or {}
            return {
                "success": True,
                "savedPath": result.get("savedPath"),
                "trainingRows": result.get("trainingRows"),
                "featureCount": result.get("featureCount"),
                "metrics": model.get("metrics"),
                "targetKey": model.get("targetKey"),
                "featureKeys": model.get("featureKeys"),
            }
        return result

    return None


_COMMANDS = {
    "train_outcome_ml":              lambda argv: handle_ml_command("train_outcome_ml", argv),
    "train_projection_ml":           lambda argv: handle_ml_command("train_projection_ml", argv),
    "train_projection_ml_per_stat":  lambda argv: handle_ml_command("train_projection_ml_per_stat", argv),
    "train_quantile_projection":     lambda argv: handle_ml_command("train_quantile_projection", argv),
    "promote_projection_ml":         lambda argv: handle_ml_command("promote_projection_ml", argv),
    "train_model":                   lambda argv: handle_ml_command("train_model", argv),
}
