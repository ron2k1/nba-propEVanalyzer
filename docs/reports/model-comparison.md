# Model Comparison

| Name | Task | Model | Filter Stats | Balanced | Holdout N | Accuracy | ROC AUC | Brier | Precision | Recall | MAE | RMSE | R2 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| production_outcome_model.pkl | outcome_classifier | gradient_boosting | ast,pts | False | 2540 | 0.5961 | 0.6302 | 0.2344 | 0.5998 | 0.8246 | - | - | - |
| outcome_pts_ast_lgb.pkl | outcome_classifier | lightgbm | ast,pts | True | 2540 | 0.5740 | 0.6066 | 0.2409 | 0.6283 | 0.5750 | - | - | - |
| outcome_pts_ast_xgb.pkl | outcome_classifier | xgboost | ast,pts | True | 2540 | 0.5559 | 0.5892 | 0.2455 | 0.6139 | 0.5453 | - | - | - |
