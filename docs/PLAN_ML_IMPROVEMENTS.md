# Plan: ML Model Improvements for NBA Prop Prediction

## Goal

Improve model accuracy through:
1. Additional gradient-boosting backends (HistGradientBoostingRegressor, XGBoost, LightGBM)
2. Per-stat ML model support
3. Quantile regression path for P(stat > line)
4. CLI wiring and quality gate verification

---

## Phase 1: Add New Estimator Backends

### 1.1 HistGradientBoostingRegressor (sklearn-native)
- **File:** `core/nba_model_ml_training.py`
- **Change:** Extend `_fit_projection_estimator()` to accept `hist_gbr`, `hgb`, `histogram_gb`
- **Why:** Sklearn-native, no new deps; often outperforms classic GBR on tabular data
- **Params:** `max_iter=200`, `learning_rate=0.1`, `max_leaf_nodes=31`, `min_samples_leaf=20`

### 1.2 XGBoost (optional dep)
- **File:** `requirements.txt` — add `xgboost>=2.0.0` (optional, in extras)
- **File:** `core/nba_model_ml_training.py` — add `xgboost`, `xgb` model type
- **Why:** Strong tabular performance; requires `pip install xgboost`
- **Fallback:** Try/except; if ImportError, return clear error message

### 1.3 LightGBM (optional dep)
- **File:** `requirements.txt` — add `lightgbm>=4.0.0` (optional)
- **File:** `core/nba_model_ml_training.py` — add `lightgbm`, `lgb` model type
- **Why:** Fast training; strong on tabular
- **Fallback:** Same try/except pattern

### 1.4 Update supported model_type string in error message
- Current: `gradient_boosting|random_forest|linear|tabpfn`
- New: `gradient_boosting|hist_gbr|xgboost|lightgbm|random_forest|linear|tabpfn`

---

## Phase 2: Per-Stat ML Model Support

### 2.1 Training data requirement
- Training rows must include `stat` column (or configurable `stat_key`)
- Filter rows by stat before train/hold split
- Per-stat: train one model per stat (pts, reb, ast, pra, etc.)

### 2.2 New function: `train_projection_ml_per_stat_from_file`
- **Args:** `data_path`, `stat_key="stat"`, `target_key="actual"`, `feature_keys`, `holdout_frac`, `model_type`, `output_dir`
- **Logic:**
  1. Load rows from file
  2. Group by `stat_key` value
  3. For each stat with enough rows (min 100 train, 25 hold), call `train_projection_ml_from_file` logic per stat
  4. Save to `{output_dir}/projection_ml_{stat}.pkl` (or single bundle with `estimators_by_stat`)
- **Bundle structure:** `{"estimatorsByStat": {"pts": {...}, "reb": {...}}, "statKey": "stat"}`

### 2.3 Prediction: `predict_projection_ml_per_stat`
- **Args:** `bundle`, `feature_row`, `stat`
- **Logic:** Look up estimator for stat; if missing, return None

### 2.4 Integration with `compute_prop_ev_with_ml`
- Add optional `use_per_stat=True` and `per_stat_bundle_path`
- When per-stat bundle loaded, use `predict_projection_ml_per_stat`

---

## Phase 3: Quantile Regression Path

### 3.1 Purpose
- Directly model P(stat > line) instead of point forecast + Normal/Poisson CDF
- Use quantile regression at multiple quantiles (e.g. 0.1, 0.25, 0.5, 0.75, 0.9) to estimate CDF
- P(stat > line) = 1 - CDF(line) ≈ 1 - interpolate(quantiles at line)

### 3.2 New function: `train_quantile_projection_from_file`
- **Args:** `data_path`, `quantiles=(0.1, 0.25, 0.5, 0.75, 0.9)`, `stat_key`, `target_key`, `feature_keys`, `model_type`
- **Logic:** For each quantile q, fit regressor predicting quantile q of target; save list of estimators
- **Bundle:** `{"quantiles": [...], "estimators": [est_q1, est_q2, ...], "featureKeys": [...]}`

### 3.3 New function: `predict_quantile_projection` and `prob_over_from_quantiles`
- Given line L and quantile predictions [q1, q2, ..., qn], interpolate to get CDF(L)
- P(stat > L) = 1 - CDF(L)

### 3.4 EV engine integration
- Add `reference_probs` alternative: when quantile model provides prob_over directly, pass as reference_probs
- Or: new `compute_ev` mode that accepts `prob_over` directly (already exists via reference_probs)

---

## Phase 4: CLI and Wiring

### 4.1 ml_commands.py
- Update `train_projection_ml` help to list new model types
- Add `train_projection_ml_per_stat` command
- Add `train_quantile_projection` command (optional, for advanced users)

### 4.2 Router
- Ensure ml_commands handle new commands
- Wire `train_projection_ml_per_stat` if added

### 4.3 Exports in nba_model_training.py
- Export new functions if they are part of public API

---

## Phase 5: Quality Gate and Verification

### 5.1 Run quality gate
- `scripts/quality_gate.py --json` must pass
- No `compute_ev()` without `stat=` in new code
- Python compile must succeed

### 5.2 Smoke tests
- `prop_ev` with existing flow unchanged
- `train_projection_ml` with `model_type=hist_gbr` (no new deps)
- If xgboost/lightgbm installed: smoke with `model_type=xgboost`

---

## Implementation Order

1. **Phase 1** — HistGradientBoostingRegressor (no new deps)
2. **Phase 1** — XGBoost, LightGBM (optional deps)
3. **Phase 2** — Per-stat model support (training + prediction)
4. **Phase 4** — CLI updates for new model types
5. **Phase 3** — Quantile regression (can be deferred or simplified)
6. **Phase 5** — Quality gate and smoke tests

---

## Files Touched

| File | Changes |
|-----|---------|
| `core/nba_model_ml_training.py` | HistGradientBoostingRegressor, XGBoost, LightGBM, per-stat, quantile |
| `requirements.txt` | Optional: xgboost, lightgbm; scikit-learn (if not already) |
| `nba_cli/ml_commands.py` | Help text, new model types |
| `core/nba_model_training.py` | Export new functions if needed |
| `docs/PLAN_ML_IMPROVEMENTS.md` | This plan |

---

## Rollback

- All new model types are additive; existing `gradient_boosting` remains default
- No breaking changes to `compute_ev` or `compute_prop_ev`
- Per-stat and quantile are opt-in

---

## Implementation Summary (2026-02-28)

### Completed

1. **HistGradientBoostingRegressor** — `model_type=hist_gbr`, `hgb`, `histogram_gb`
2. **XGBoost** — `model_type=xgboost`, `xgb` (requires `pip install xgboost`)
3. **LightGBM** — `model_type=lightgbm`, `lgb` (requires `pip install lightgbm`)
4. **Per-stat ML** — `train_projection_ml_per_stat` CLI; `train_projection_ml_per_stat_from_file()`; `predict_projection_ml_per_stat()`; `compute_prop_ev_with_ml(per_stat_model_path=...)`
5. **Quantile regression** — `train_quantile_projection` CLI; `train_quantile_projection_from_file()`; `predict_quantile_projection()`; `prob_over_from_quantiles()`
6. **Dependencies** — Added `scikit-learn`, `xgboost`, `lightgbm` to `requirements.txt`
7. **Quality gate** — Passes; `prop_ev` smoke test OK

### CLI Commands

```
train_projection_ml <data_path> [target_key] [feature_keys|auto] [holdout_frac] [min_holdout] [model_type] [date_key] [output_path]
  model_type: gradient_boosting|hist_gbr|xgboost|lightgbm|random_forest|linear|tabpfn

train_projection_ml_per_stat <data_path> [stat_key] [target_key] [holdout_frac] [min_holdout] [model_type] [output_path]
  Rows must have stat_key column (e.g. stat=pts,reb,ast)

train_quantile_projection <data_path> [quantiles_csv] [output_path]
  quantiles default: 0.1,0.25,0.5,0.75,0.9
```
