#!/usr/bin/env python3
"""Compatibility facade for modeling logic.

Primary implementations now live in:
- nba_ev_engine.py
- nba_prop_engine.py
- nba_parlay_engine.py
- nba_model_ml_training.py
"""

from .nba_ev_engine import (
    american_to_decimal,
    american_to_implied_prob,
    compute_ev,
    prob_to_american,
)
from .nba_model_ml_training import (
    DEFAULT_OUTCOME_ML_MODEL_PATH,
    DEFAULT_PROJECTION_ML_MODEL_PATH,
    build_outcome_feature_row,
    compute_prop_ev_with_ml,
    default_outcome_feature_keys,
    infer_feature_keys,
    infer_projection_feature_keys,
    load_outcome_ml_bundle,
    load_projection_ml_bundle,
    load_training_rows,
    predict_outcome_ml,
    predict_projection_ml,
    predict_projection_ml_per_stat,
    predict_quantile_projection,
    predict_ridge_calibrator,
    prob_over_from_quantiles,
    promote_projection_ml_model,
    score_rows_with_outcome_ml,
    train_outcome_ml_from_file,
    train_projection_ml_from_file,
    train_projection_ml_per_stat_from_file,
    train_quantile_projection_from_file,
    train_ridge_calibrator,
    train_ridge_calibrator_from_file,
)
from .nba_parlay_engine import compute_parlay_ev
from .nba_prop_engine import compute_auto_line_sweep, compute_live_projection, compute_prop_ev

__all__ = [
    "DEFAULT_OUTCOME_ML_MODEL_PATH",
    "DEFAULT_PROJECTION_ML_MODEL_PATH",
    "american_to_decimal",
    "american_to_implied_prob",
    "build_outcome_feature_row",
    "compute_auto_line_sweep",
    "compute_ev",
    "compute_live_projection",
    "compute_parlay_ev",
    "compute_prop_ev",
    "compute_prop_ev_with_ml",
    "default_outcome_feature_keys",
    "infer_feature_keys",
    "infer_projection_feature_keys",
    "load_outcome_ml_bundle",
    "load_projection_ml_bundle",
    "load_training_rows",
    "predict_outcome_ml",
    "predict_projection_ml",
    "predict_projection_ml_per_stat",
    "predict_quantile_projection",
    "predict_ridge_calibrator",
    "prob_over_from_quantiles",
    "prob_to_american",
    "promote_projection_ml_model",
    "score_rows_with_outcome_ml",
    "train_outcome_ml_from_file",
    "train_projection_ml_from_file",
    "train_projection_ml_per_stat_from_file",
    "train_quantile_projection_from_file",
    "train_ridge_calibrator",
    "train_ridge_calibrator_from_file",
]
