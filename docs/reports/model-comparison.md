# Model Comparison

## Outcome Classifiers (win/loss prediction)

| Name | Filter Stats | Calibrated | Holdout N | Accuracy | ROC AUC | Brier | LogLoss |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GBC all-stats isotonic (production) | all | isotonic | 11,338 | 0.6341 | 0.6478 | **0.2195** | **0.6270** |
| GBC all-stats no-cal | all | no | 11,338 | 0.6328 | 0.6456 | 0.2200 | 0.6275 |
| GBC pts+ast no-cal | pts,ast | no | 2,540 | 0.5961 | 0.6302 | 0.2344 | 0.6603 |
| GBC pts+ast isotonic | pts,ast | isotonic | 2,540 | 0.5795 | 0.6236 | 0.2362 | 0.6645 |
| LightGBM pts+ast balanced | pts,ast | no | 2,540 | 0.5740 | 0.6066 | 0.2409 | - |
| XGBoost pts+ast balanced | pts,ast | no | 2,540 | 0.5559 | 0.5892 | 0.2455 | - |

**Key finding:** All-stats model outperforms pts+ast-filtered model. 56K rows > 12.7K rows matters more than stat specificity. Isotonic calibration helps on large datasets, hurts on small.

## Per-Stat Projection Regression (C.1)

Predicting actual stat value from pick-time features.

| Stat | Raw Projection MAE | ML MAE (clean features) | ML MAE (leaky) | R2 (clean) |
| --- | --- | --- | --- | --- |
| pts | 4.6624 | 5.1050 | 3.1428 | 0.2487 |
| ast | 1.3444 | 1.4941 | 1.1584 | 0.2841 |

**Verdict:** With clean (non-leaking) features, ML per-stat regression does NOT beat raw projection. The Bayesian shrinkage + adjustment system already captures the learnable signal from pick-time features. Leaky features (closingLine, pnl, clvDelta) gave false positive improvements.

## Quantile Regression (C.2)

Estimating P(stat > line) by interpolating quantile predictions vs Normal CDF assumption.

| Method | Holdout N | Brier Score | Improvement |
| --- | --- | --- | --- |
| Normal CDF | 11,338 | 0.2249 | baseline |
| Quantile regression (clean features) | 11,338 | 0.2216 | +1.49% |

**Verdict:** Modest but real improvement. Quantile regression captures skewness that Normal CDF misses. Worth further investigation with per-stat quantile models.

## Recommendations

1. **Production model:** GBC all-stats with isotonic calibration (Brier 0.2195)
2. **Do not filter to pts+ast** for the classifier — stat one-hot features let the model learn stat-specific patterns
3. **Per-stat projection regression is a dead end** with current features — the Bayesian engine already uses the same information
4. **Quantile regression** shows promise but needs per-stat training and integration testing
5. **Next steps:** Use outcome model as informational enrichment (outcomeModelWinProb in prop_ev output), not as a gate
