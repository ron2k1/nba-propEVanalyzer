#!/usr/bin/env python3
"""ML training and model-gating utilities."""

import csv
import json
import os
import pickle
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np

from .nba_data_collection import safe_round
from .nba_ev_engine import american_to_implied_prob, compute_ev
from .nba_prop_engine import compute_prop_ev

DEFAULT_PROJECTION_ML_MODEL_PATH = str(
    (Path(__file__).resolve().parent.parent / "models" / "production_projection_model.pkl")
)


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_date_any(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%b %d, %Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _time_split_rows(rows, date_key="pickDate", holdout_frac=0.2, min_holdout=50):
    dated = []
    undated = []
    for r in rows:
        dt = _parse_date_any(r.get(date_key))
        if dt is None:
            undated.append(r)
        else:
            dated.append((dt, r))

    dated.sort(key=lambda x: x[0])
    ordered = [r for _, r in dated] + undated
    n = len(ordered)
    n_hold = max(int(round(n * float(holdout_frac))), int(min_holdout))
    n_hold = min(max(1, n_hold), max(1, n - 1))
    split_idx = max(1, n - n_hold)
    return ordered[:split_idx], ordered[split_idx:]


def _prepare_xy(rows, feature_keys, target_key):
    X = []
    y = []
    kept = []
    for r in rows:
        t = _to_float(r.get(target_key))
        if t is None:
            continue
        vec = []
        bad = False
        for k in feature_keys:
            v = _to_float(r.get(k))
            if v is None:
                bad = True
                break
            vec.append(v)
        if bad:
            continue
        X.append(vec)
        y.append(t)
        kept.append(r)
    return np.array(X, dtype=float), np.array(y, dtype=float), kept


def _regression_metrics(y_true, y_pred):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    if y_true.size == 0:
        return {"count": 0, "mae": None, "rmse": None, "r2": None}

    err = y_pred - y_true
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    ss_res = float(np.sum(err ** 2))
    y_mean = float(np.mean(y_true))
    ss_tot = float(np.sum((y_true - y_mean) ** 2))
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else None
    return {
        "count": int(y_true.size),
        "mae": safe_round(mae, 6),
        "rmse": safe_round(rmse, 6),
        "r2": safe_round(r2, 6) if r2 is not None else None,
    }


def infer_projection_feature_keys(rows, target_key="actual", min_non_null=10):
    if not rows:
        return []

    numeric_counts = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k, v in r.items():
            if k == target_key:
                continue
            fv = _to_float(v)
            if fv is None:
                continue
            numeric_counts[k] = numeric_counts.get(k, 0) + 1

    blocked_prefixes = ("entry", "created", "settled", "actualGame", "playerName", "result", "source")
    blocked_exact = {"pickDate", "line", "overOdds", "underOdds", "recommendedOdds"}

    keys = []
    for k, cnt in numeric_counts.items():
        if cnt < int(min_non_null):
            continue
        lk = str(k).lower()
        if k in blocked_exact:
            continue
        if any(lk.startswith(bp) for bp in blocked_prefixes):
            continue
        keys.append(k)

    preferred = [
        "projection",
        "projStdev",
        "lineDiff",
        "probOver",
        "probUnder",
        "probPush",
        "impliedOverProb",
        "impliedUnderProb",
        "evOverPct",
        "evUnderPct",
        "bestEvPct",
        "bestIsOver",
        "isHome",
        "isB2B",
    ]
    pref_set = {k for k in preferred if k in keys}
    ordered = [k for k in preferred if k in pref_set]
    tail = sorted([k for k in keys if k not in pref_set])
    return ordered + tail


def _fit_projection_estimator(X_train, y_train, model_type="gradient_boosting", random_state=42):
    mt = str(model_type or "gradient_boosting").lower().strip()

    if mt in {"tabpfn"}:
        try:
            from tabpfn import TabPFNRegressor
        except ImportError:
            return None, (
                "tabpfn is required for model_type='tabpfn'. "
                "Install with: .\\.venv\\Scripts\\python.exe -m pip install tabpfn"
            )
        est = TabPFNRegressor(random_state=random_state)
        est.fit(X_train, y_train)
        return est, None

    # XGBoost (optional dep)
    if mt in {"xgboost", "xgb"}:
        try:
            import xgboost as xgb
        except ImportError:
            return None, (
                "xgboost is required for model_type='xgboost'. "
                "Install with: .\\.venv\\Scripts\\python.exe -m pip install xgboost"
            )
        est = xgb.XGBRegressor(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.1,
            random_state=random_state,
            n_jobs=-1,
        )
        est.fit(X_train, y_train)
        return est, None

    # LightGBM (optional dep)
    if mt in {"lightgbm", "lgb"}:
        try:
            import lightgbm as lgb
        except ImportError:
            return None, (
                "lightgbm is required for model_type='lightgbm'. "
                "Install with: .\\.venv\\Scripts\\python.exe -m pip install lightgbm"
            )
        est = lgb.LGBMRegressor(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.1,
            random_state=random_state,
            n_jobs=-1,
            verbose=-1,
        )
        est.fit(X_train, y_train)
        return est, None

    try:
        from sklearn.ensemble import (
            GradientBoostingRegressor,
            HistGradientBoostingRegressor,
            RandomForestRegressor,
        )
        from sklearn.linear_model import LinearRegression
    except Exception:
        return None, (
            "scikit-learn is required for train_projection_ml. "
            "Install with: .\\.venv\\Scripts\\python.exe -m pip install scikit-learn"
        )

    if mt in {"gbr", "gradient_boosting", "gb"}:
        est = GradientBoostingRegressor(random_state=random_state)
    elif mt in {"hist_gbr", "hgb", "histogram_gb"}:
        est = HistGradientBoostingRegressor(
            max_iter=200,
            learning_rate=0.1,
            max_leaf_nodes=31,
            min_samples_leaf=20,
            random_state=random_state,
        )
    elif mt in {"rf", "random_forest"}:
        est = RandomForestRegressor(
            n_estimators=400,
            min_samples_leaf=2,
            random_state=random_state,
            n_jobs=-1,
        )
    elif mt in {"linear", "linreg"}:
        est = LinearRegression()
    else:
        return None, (
            f"Unsupported model_type '{model_type}'. Use gradient_boosting|hist_gbr|"
            "xgboost|lightgbm|random_forest|linear|tabpfn."
        )

    est.fit(X_train, y_train)
    return est, None


def train_projection_ml_from_file(
    data_path,
    target_key="actual",
    feature_keys=None,
    holdout_frac=0.2,
    min_holdout=50,
    model_type="gradient_boosting",
    date_key="pickDate",
    output_model_path=None,
):
    rows_result = load_training_rows(data_path)
    if not rows_result.get("success"):
        return rows_result
    rows = rows_result["rows"]
    if len(rows) < 200:
        return {"success": False, "error": f"Need at least 200 rows, found {len(rows)}."}

    if not feature_keys:
        feature_keys = infer_projection_feature_keys(rows, target_key=target_key, min_non_null=50)
    if not feature_keys:
        return {"success": False, "error": "Could not infer usable feature keys."}

    train_rows, hold_rows = _time_split_rows(
        rows, date_key=date_key, holdout_frac=holdout_frac, min_holdout=min_holdout
    )
    X_train, y_train, kept_train = _prepare_xy(train_rows, feature_keys, target_key)
    X_hold, y_hold, kept_hold = _prepare_xy(hold_rows, feature_keys, target_key)
    if len(y_train) < 100 or len(y_hold) < 25:
        return {
            "success": False,
            "error": "Insufficient valid rows after NA filtering.",
            "trainValid": int(len(y_train)),
            "holdoutValid": int(len(y_hold)),
        }

    est, err = _fit_projection_estimator(X_train, y_train, model_type=model_type)
    if err:
        return {"success": False, "error": err}

    train_pred = est.predict(X_train)
    hold_pred = est.predict(X_hold)
    train_metrics = _regression_metrics(y_train, train_pred)
    hold_metrics = _regression_metrics(y_hold, hold_pred)

    payload = {
        "modelType": str(model_type),
        "featureKeys": list(feature_keys),
        "targetKey": str(target_key),
        "dateKey": str(date_key),
        "trainedAtUtc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "metrics": {
            "train": train_metrics,
            "holdout": hold_metrics,
            "nRowsTotal": int(len(rows)),
            "nRowsTrainRaw": int(len(train_rows)),
            "nRowsHoldoutRaw": int(len(hold_rows)),
            "nRowsTrainUsed": int(len(kept_train)),
            "nRowsHoldoutUsed": int(len(kept_hold)),
        },
        "estimator": est,
    }

    if output_model_path is None:
        out = Path(data_path).resolve().with_suffix("")
        output_model_path = str(out) + "_projection_ml.pkl"
    out_path = Path(output_model_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "wb") as f:
        pickle.dump(payload, f)

    return {
        "success": True,
        "outputModelPath": str(out_path),
        "modelType": payload["modelType"],
        "featureKeys": payload["featureKeys"],
        "targetKey": payload["targetKey"],
        "metrics": payload["metrics"],
    }


def train_projection_ml_per_stat_from_file(
    data_path,
    stat_key="stat",
    target_key="actual",
    feature_keys=None,
    holdout_frac=0.2,
    min_holdout=25,
    min_train=100,
    model_type="gradient_boosting",
    date_key="pickDate",
    output_model_path=None,
):
    """Train separate ML models per stat (pts, reb, ast, etc.). Rows must include stat_key column."""
    rows_result = load_training_rows(data_path)
    if not rows_result.get("success"):
        return rows_result
    rows = rows_result["rows"]

    # Group rows by stat
    by_stat = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        stat_val = r.get(stat_key)
        if stat_val is None or (isinstance(stat_val, str) and not stat_val.strip()):
            continue
        stat_val = str(stat_val).strip().lower()
        by_stat.setdefault(stat_val, []).append(r)

    if not by_stat:
        return {"success": False, "error": f"No rows with valid '{stat_key}' column."}

    # Shared feature keys inferred from all rows (excluding stat_key from features)
    if not feature_keys:
        feature_keys = infer_projection_feature_keys(
            rows, target_key=target_key, min_non_null=30
        )
        if stat_key in feature_keys:
            feature_keys = [k for k in feature_keys if k != stat_key]
    if not feature_keys:
        return {"success": False, "error": "Could not infer usable feature keys."}

    estimators_by_stat = {}
    metrics_by_stat = {}

    for stat_val, stat_rows in sorted(by_stat.items()):
        if len(stat_rows) < min_train + min_holdout:
            continue
        train_rows, hold_rows = _time_split_rows(
            stat_rows, date_key=date_key, holdout_frac=holdout_frac, min_holdout=min_holdout
        )
        X_train, y_train, kept_train = _prepare_xy(train_rows, feature_keys, target_key)
        X_hold, y_hold, kept_hold = _prepare_xy(hold_rows, feature_keys, target_key)
        if len(y_train) < min_train or len(y_hold) < min_holdout:
            continue

        est, err = _fit_projection_estimator(X_train, y_train, model_type=model_type)
        if err:
            continue

        train_pred = est.predict(X_train)
        hold_pred = est.predict(X_hold)
        train_metrics = _regression_metrics(y_train, train_pred)
        hold_metrics = _regression_metrics(y_hold, hold_pred)

        estimators_by_stat[stat_val] = {
            "estimator": est,
            "featureKeys": list(feature_keys),
            "metrics": {
                "train": train_metrics,
                "holdout": hold_metrics,
                "nRowsTrainUsed": int(len(kept_train)),
                "nRowsHoldoutUsed": int(len(kept_hold)),
            },
        }
        metrics_by_stat[stat_val] = hold_metrics

    if not estimators_by_stat:
        return {
            "success": False,
            "error": (
                f"No stat had enough rows (min {min_train} train, {min_holdout} hold). "
                f"Stats found: {list(by_stat.keys())}"
            ),
        }

    if output_model_path is None:
        base = Path(data_path).resolve().with_suffix("")
        output_model_path = str(base) + "_projection_ml_per_stat.pkl"
    out_path = Path(output_model_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "perStat": True,
        "statKey": stat_key,
        "modelType": str(model_type),
        "targetKey": target_key,
        "dateKey": date_key,
        "trainedAtUtc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "estimatorsByStat": estimators_by_stat,
        "metricsByStat": metrics_by_stat,
    }
    with open(out_path, "wb") as f:
        pickle.dump(payload, f)

    return {
        "success": True,
        "outputModelPath": str(out_path),
        "perStat": True,
        "statsTrained": list(estimators_by_stat.keys()),
        "modelType": payload["modelType"],
        "metricsByStat": metrics_by_stat,
    }


def load_projection_ml_bundle(model_path):
    p = Path(model_path).expanduser().resolve()
    if not p.exists():
        return {"success": False, "error": f"Model file not found: {p}"}
    try:
        with open(p, "rb") as f:
            bundle = pickle.load(f)
        bundle["modelPath"] = str(p)
        return {"success": True, "bundle": bundle}
    except Exception as e:
        return {"success": False, "error": str(e)}


def predict_projection_ml(bundle, feature_row):
    est = (bundle or {}).get("estimator")
    keys = (bundle or {}).get("featureKeys") or []
    if est is None or not keys:
        return None
    vec = []
    for k in keys:
        v = _to_float((feature_row or {}).get(k))
        if v is None:
            return None
        vec.append(v)
    arr = np.array([vec], dtype=float)
    pred = est.predict(arr)
    return float(pred[0]) if len(pred) else None


def predict_projection_ml_per_stat(bundle, feature_row, stat):
    """Predict using per-stat bundle. stat should match the key used at training (e.g. 'pts', 'reb')."""
    if not bundle or not bundle.get("perStat"):
        return None
    stat_key = str(stat or "").strip().lower()
    if not stat_key:
        return None
    sub = (bundle.get("estimatorsByStat") or {}).get(stat_key)
    if not sub:
        return None
    return predict_projection_ml(sub, feature_row)


def _build_projection_feature_row_for_prop(prop_data):
    proj = (prop_data or {}).get("projection") or {}
    ev = (prop_data or {}).get("ev") or {}
    over = ev.get("over") or {}
    under = ev.get("under") or {}
    ev_over_pct = _to_float(over.get("evPercent"), 0.0)
    ev_under_pct = _to_float(under.get("evPercent"), 0.0)
    best_is_over = 1.0 if ev_over_pct >= ev_under_pct else 0.0
    best_ev_pct = ev_over_pct if best_is_over else ev_under_pct

    line = _to_float(prop_data.get("line"), 0.0)
    projection_val = _to_float(proj.get("projection"), 0.0)
    over_odds = _to_float((prop_data or {}).get("bestOverOdds") or (prop_data or {}).get("overOdds"), None)
    under_odds = _to_float((prop_data or {}).get("bestUnderOdds") or (prop_data or {}).get("underOdds"), None)
    implied_over = american_to_implied_prob(over_odds) if over_odds is not None else None
    implied_under = american_to_implied_prob(under_odds) if under_odds is not None else None

    return {
        "projection": projection_val,
        "projStdev": _to_float(proj.get("projStdev") or proj.get("stdev"), 0.0),
        "line": line,
        "lineDiff": safe_round(projection_val - line, 4),
        "overOdds": over_odds,
        "underOdds": under_odds,
        "impliedOverProb": implied_over if implied_over is not None else 0.0,
        "impliedUnderProb": implied_under if implied_under is not None else 0.0,
        "probOver": _to_float(ev.get("probOver"), 0.0),
        "probUnder": _to_float(ev.get("probUnder"), 0.0),
        "probPush": _to_float(ev.get("probPush"), 0.0),
        "evOverPct": ev_over_pct,
        "evUnderPct": ev_under_pct,
        "bestEvPct": best_ev_pct,
        "bestIsOver": best_is_over,
        "isHome": 1 if bool(prop_data.get("isHome")) else 0,
        "isB2B": 1 if bool(prop_data.get("isB2B")) else 0,
    }


def compute_prop_ev_with_ml(
    player_id,
    opponent_abbr,
    is_home,
    stat,
    line,
    over_odds,
    under_odds,
    is_b2b=False,
    season=None,
    model_path=DEFAULT_PROJECTION_ML_MODEL_PATH,
    per_stat_model_path=None,
):
    base = compute_prop_ev(
        player_id=player_id,
        opponent_abbr=opponent_abbr,
        is_home=is_home,
        stat=stat,
        line=line,
        over_odds=over_odds,
        under_odds=under_odds,
        is_b2b=is_b2b,
        season=season,
    )
    if not base.get("success"):
        return base

    base["overOdds"] = over_odds
    base["underOdds"] = under_odds

    path_to_load = per_stat_model_path if per_stat_model_path else model_path
    loaded = load_projection_ml_bundle(path_to_load)
    if not loaded.get("success"):
        return {**base, "mlProjection": None, "mlEv": None, "mlModelError": loaded.get("error")}

    bundle = loaded["bundle"]
    feature_row = _build_projection_feature_row_for_prop(base)
    if bundle.get("perStat") and stat:
        ml_projection = predict_projection_ml_per_stat(bundle, feature_row, stat)
    else:
        ml_projection = predict_projection_ml(bundle, feature_row)
    if ml_projection is None:
        return {**base, "mlProjection": None, "mlEv": None, "mlModelError": "ML prediction failed for feature row."}

    stdev_val = _to_float((base.get("projection") or {}).get("projStdev"), 0.0) or _to_float(
        (base.get("projection") or {}).get("stdev"), 0.0
    )
    ml_over_odds = int(base.get("bestOverOdds") or over_odds)
    ml_under_odds = int(base.get("bestUnderOdds") or under_odds)
    ml_ev = compute_ev(
        ml_projection, line, ml_over_odds, ml_under_odds,
        stdev_val, stat=stat,
    )

    return {
        **base,
        "mlModelPath": loaded.get("bundle", {}).get("modelPath"),
        "mlProjection": safe_round(ml_projection, 3),
        "mlEv": ml_ev,
    }


def promote_projection_ml_model(
    candidate_model_path,
    production_model_path=DEFAULT_PROJECTION_ML_MODEL_PATH,
    min_rmse_improve_pct=1.0,
    min_mae_improve_pct=1.0,
    force=False,
):
    cand_loaded = load_projection_ml_bundle(candidate_model_path)
    if not cand_loaded.get("success"):
        return cand_loaded

    candidate = cand_loaded["bundle"]
    cand_hold = ((candidate.get("metrics") or {}).get("holdout") or {})
    cand_rmse = _to_float(cand_hold.get("rmse"))
    cand_mae = _to_float(cand_hold.get("mae"))
    if cand_rmse is None or cand_mae is None:
        return {"success": False, "error": "Candidate model missing holdout rmse/mae metrics."}

    prod_path = Path(production_model_path).expanduser().resolve()
    prod_path.parent.mkdir(parents=True, exist_ok=True)
    decision = {
        "candidatePath": str(Path(candidate_model_path).expanduser().resolve()),
        "productionPath": str(prod_path),
        "force": bool(force),
        "thresholds": {
            "minRmseImprovePct": float(min_rmse_improve_pct),
            "minMaeImprovePct": float(min_mae_improve_pct),
        },
    }

    if not prod_path.exists() or force:
        shutil.copy2(decision["candidatePath"], decision["productionPath"])
        decision["action"] = "promoted"
        decision["reason"] = "no_production_model" if not prod_path.exists() else "force"
        return {"success": True, "decision": decision}

    prod_loaded = load_projection_ml_bundle(str(prod_path))
    if not prod_loaded.get("success"):
        return {"success": False, "error": f"Failed loading production model: {prod_loaded.get('error')}"}

    prod_hold = (((prod_loaded.get("bundle") or {}).get("metrics") or {}).get("holdout") or {})
    prod_rmse = _to_float(prod_hold.get("rmse"))
    prod_mae = _to_float(prod_hold.get("mae"))
    if prod_rmse is None or prod_mae is None:
        return {"success": False, "error": "Production model missing holdout rmse/mae metrics."}

    rmse_improve = ((prod_rmse - cand_rmse) / prod_rmse) * 100.0 if prod_rmse > 0 else 0.0
    mae_improve = ((prod_mae - cand_mae) / prod_mae) * 100.0 if prod_mae > 0 else 0.0
    decision["metrics"] = {
        "candidate": {"rmse": cand_rmse, "mae": cand_mae},
        "production": {"rmse": prod_rmse, "mae": prod_mae},
        "improvementPct": {"rmse": safe_round(rmse_improve, 4), "mae": safe_round(mae_improve, 4)},
    }

    if rmse_improve >= float(min_rmse_improve_pct) and mae_improve >= float(min_mae_improve_pct):
        shutil.copy2(decision["candidatePath"], decision["productionPath"])
        decision["action"] = "promoted"
        decision["reason"] = "meets_thresholds"
    else:
        decision["action"] = "rejected"
        decision["reason"] = "below_thresholds"
    return {"success": True, "decision": decision}


def train_quantile_projection_from_file(
    data_path,
    quantiles=(0.1, 0.25, 0.5, 0.75, 0.9),
    target_key="actual",
    feature_keys=None,
    holdout_frac=0.2,
    min_holdout=50,
    date_key="pickDate",
    output_model_path=None,
):
    """Train quantile regressors to estimate full distribution. Use prob_over_from_quantiles for P(stat > line)."""
    rows_result = load_training_rows(data_path)
    if not rows_result.get("success"):
        return rows_result
    rows = rows_result["rows"]
    if len(rows) < 200:
        return {"success": False, "error": f"Need at least 200 rows, found {len(rows)}."}

    if not feature_keys:
        feature_keys = infer_projection_feature_keys(rows, target_key=target_key, min_non_null=50)
    if not feature_keys:
        return {"success": False, "error": "Could not infer usable feature keys."}

    train_rows, hold_rows = _time_split_rows(
        rows, date_key=date_key, holdout_frac=holdout_frac, min_holdout=min_holdout
    )
    X_train, y_train, _ = _prepare_xy(train_rows, feature_keys, target_key)
    X_hold, y_hold, _ = _prepare_xy(hold_rows, feature_keys, target_key)
    if len(y_train) < 100 or len(y_hold) < 25:
        return {
            "success": False,
            "error": "Insufficient valid rows after NA filtering.",
        }

    try:
        from sklearn.ensemble import GradientBoostingRegressor
    except Exception:
        return {"success": False, "error": "scikit-learn required for quantile regression."}

    estimators = []
    qs = sorted(set(float(q) for q in quantiles))
    for q in qs:
        est = GradientBoostingRegressor(
            loss="quantile",
            alpha=q,
            n_estimators=150,
            max_depth=5,
            random_state=42,
        )
        est.fit(X_train, y_train)
        estimators.append((q, est))

    if output_model_path is None:
        base = Path(data_path).resolve().with_suffix("")
        output_model_path = str(base) + "_quantile_projection.pkl"
    out_path = Path(output_model_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "quantile": True,
        "quantiles": qs,
        "estimators": estimators,
        "featureKeys": list(feature_keys),
        "targetKey": target_key,
        "trainedAtUtc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }
    with open(out_path, "wb") as f:
        pickle.dump(payload, f)

    # Evaluate: median prediction MAE
    med_idx = next((i for i, (q, _) in enumerate(estimators) if q >= 0.5), len(estimators) - 1)
    med_est = estimators[min(med_idx, len(estimators) - 1)][1]
    hold_pred = med_est.predict(X_hold)
    hold_mae = float(np.mean(np.abs(np.array(y_hold) - hold_pred)))

    return {
        "success": True,
        "outputModelPath": str(out_path),
        "quantiles": qs,
        "holdoutMaeMedian": safe_round(hold_mae, 4),
    }


def predict_quantile_projection(bundle, feature_row):
    """Return dict of quantile -> predicted value. Bundle must have quantile=True."""
    if not bundle or not bundle.get("quantile"):
        return None
    keys = bundle.get("featureKeys") or []
    estimators = bundle.get("estimators") or []
    if not keys or not estimators:
        return None
    vec = []
    for k in keys:
        v = _to_float((feature_row or {}).get(k))
        if v is None:
            return None
        vec.append(v)
    arr = np.array([vec], dtype=float)
    out = {}
    for q, est in estimators:
        pred = est.predict(arr)
        out[float(q)] = float(pred[0]) if len(pred) else None
    return out


def prob_over_from_quantiles(line, quantile_preds):
    """Interpolate CDF at line from quantile predictions; return P(stat > line) = 1 - CDF(line)."""
    if not quantile_preds or len(quantile_preds) < 2:
        return None
    qs = sorted(quantile_preds.keys())
    vals = [quantile_preds[q] for q in qs]
    line_val = float(line)
    if line_val <= vals[0]:
        return 1.0 - qs[0]
    if line_val >= vals[-1]:
        return 1.0 - qs[-1]
    for i in range(len(qs) - 1):
        if vals[i] <= line_val <= vals[i + 1]:
            # Linear interpolate CDF between qs[i] and qs[i+1]
            t = (line_val - vals[i]) / (vals[i + 1] - vals[i]) if vals[i + 1] != vals[i] else 1.0
            cdf = qs[i] + t * (qs[i + 1] - qs[i])
            return 1.0 - cdf
    return None


def train_ridge_calibrator(rows, feature_keys, target_key="actual", ridge_alpha=0.5):
    if not rows:
        return {"success": False, "error": "No rows provided"}
    if not feature_keys:
        return {"success": False, "error": "feature_keys is empty"}

    X = []
    y = []
    kept = 0
    for row in rows:
        target_val = _to_float(row.get(target_key))
        if target_val is None:
            continue
        vec = []
        bad = False
        for key in feature_keys:
            f = _to_float(row.get(key))
            if f is None:
                bad = True
                break
            vec.append(float(f))
        if bad:
            continue
        X.append(vec)
        y.append(float(target_val))
        kept += 1

    if kept < 10:
        return {"success": False, "error": f"Not enough usable rows ({kept}) to train model"}

    X_arr = np.array(X, dtype=float)
    y_arr = np.array(y, dtype=float)
    n, d = X_arr.shape
    X_aug = np.hstack([np.ones((n, 1)), X_arr])
    I = np.eye(d + 1, dtype=float)
    I[0, 0] = 0.0
    alpha = float(ridge_alpha)
    beta = np.linalg.solve(X_aug.T @ X_aug + alpha * I, X_aug.T @ y_arr)
    preds = X_aug @ beta
    residuals = preds - y_arr
    mae = float(np.mean(np.abs(residuals)))
    rmse = float(np.sqrt(np.mean(residuals ** 2)))
    ss_res = float(np.sum(residuals ** 2))
    y_mean = float(np.mean(y_arr))
    ss_tot = float(np.sum((y_arr - y_mean) ** 2))
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else None

    model = {
        "type": "ridge_calibrator",
        "version": 1,
        "trainedAtUtc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "featureKeys": list(feature_keys),
        "targetKey": str(target_key),
        "ridgeAlpha": alpha,
        "coefficients": {
            "intercept": float(beta[0]),
            "weights": {k: float(beta[i + 1]) for i, k in enumerate(feature_keys)},
        },
        "metrics": {"trainRows": int(n), "mae": mae, "rmse": rmse, "r2": r2},
    }
    return {"success": True, "model": model}


def predict_ridge_calibrator(model, feature_row):
    if not model or model.get("type") != "ridge_calibrator":
        return None
    coeffs = model.get("coefficients", {})
    pred = _to_float(coeffs.get("intercept"), 0.0) or 0.0
    for key in model.get("featureKeys", []):
        w = _to_float((coeffs.get("weights", {}) or {}).get(key))
        x = _to_float((feature_row or {}).get(key))
        if w is None or x is None:
            return None
        pred += w * x
    return float(pred)


def load_training_rows(data_path):
    p = Path(data_path).expanduser().resolve()
    if not p.exists():
        return {"success": False, "error": f"Training data file not found: {p}"}
    suffix = p.suffix.lower()
    rows = []
    try:
        if suffix == ".csv":
            with open(p, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        elif suffix in {".json", ".jsonl"}:
            with open(p, "r", encoding="utf-8") as f:
                if suffix == ".jsonl":
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        rows.append(json.loads(line))
                else:
                    payload = json.load(f)
                    if isinstance(payload, list):
                        rows = payload
                    elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
                        rows = payload["rows"]
                    else:
                        return {"success": False, "error": "JSON training file must be a list or {'rows':[...]}."}
        else:
            return {"success": False, "error": f"Unsupported file type: {suffix}. Use csv/json/jsonl."}
    except Exception as e:
        return {"success": False, "error": str(e)}

    if not rows:
        return {"success": False, "error": "Training file is empty or unreadable rows."}
    return {"success": True, "rows": rows, "path": str(p), "rowCount": len(rows)}


def infer_feature_keys(rows, target_key="actual", exclude_keys=None, min_non_null=10):
    if not rows:
        return []
    blocked = set(exclude_keys or [])
    blocked.add(target_key)
    counts = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            if k in blocked:
                continue
            if _to_float(v) is not None:
                counts[k] = counts.get(k, 0) + 1
    keys = [k for k, c in counts.items() if c >= int(min_non_null)]
    keys.sort()
    return keys


def train_ridge_calibrator_from_file(
    data_path,
    target_key="actual",
    feature_keys=None,
    ridge_alpha=0.5,
    output_model_path=None,
):
    loaded = load_training_rows(data_path)
    if not loaded.get("success"):
        return loaded
    rows = loaded["rows"]

    if feature_keys is None:
        feature_keys = infer_feature_keys(
            rows,
            target_key=target_key,
            exclude_keys={"playerName", "stat", "result", "pickDate", "createdAtLocal", "createdAtUtc"},
            min_non_null=20,
        )
    if not feature_keys:
        return {"success": False, "error": "No usable feature keys found/inferred."}

    trained = train_ridge_calibrator(rows, feature_keys, target_key=target_key, ridge_alpha=ridge_alpha)
    if not trained.get("success"):
        return trained
    model = trained["model"]

    if output_model_path is None:
        base = Path(data_path).resolve().with_suffix("")
        output_model_path = str(base) + "_ridge_model.json"
    out = Path(output_model_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=2)

    return {
        "success": True,
        "outputModelPath": str(out),
        "featureKeys": model.get("featureKeys", []),
        "targetKey": model.get("targetKey"),
        "ridgeAlpha": model.get("ridgeAlpha"),
        "metrics": model.get("metrics", {}),
    }
