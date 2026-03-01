#!/usr/bin/env python3
"""
Small-window parity check between local indexed data and live NBA API source.

Use this before trusting a new local index build:
  python scripts/parity_local_vs_nba.py 2022-01-01 2022-01-02 --model simple --local-index data/reference/kaggle_nba/index.pkl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.nba_backtest import TRACKED_STATS, run_backtest


def _model_names(model: str):
    return ["full", "simple"] if model == "both" else [model]


def _report_delta(local_r: dict, nba_r: dict) -> dict:
    out = {}
    for stat in TRACKED_STATS:
        l_mae = (local_r.get("maeByStat") or {}).get(stat)
        n_mae = (nba_r.get("maeByStat") or {}).get(stat)
        l_brier = (local_r.get("brierByStat") or {}).get(stat)
        n_brier = (nba_r.get("brierByStat") or {}).get(stat)
        out[stat] = {
            "maeLocal": l_mae,
            "maeNba": n_mae,
            "maeDeltaLocalMinusNba": (None if l_mae is None or n_mae is None else round(float(l_mae) - float(n_mae), 6)),
            "brierLocal": l_brier,
            "brierNba": n_brier,
            "brierDeltaLocalMinusNba": (None if l_brier is None or n_brier is None else round(float(l_brier) - float(n_brier), 6)),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare local vs nba data-source backtest metrics on a small date window")
    parser.add_argument("date_from", help="YYYY-MM-DD")
    parser.add_argument("date_to", help="YYYY-MM-DD")
    parser.add_argument("--model", default="simple", choices=["simple", "full", "both"], help="Backtest model variant")
    parser.add_argument("--local-index", default=None, help="Optional custom local index path")
    parser.add_argument("--save", action="store_true", help="Write parity report to data/backtest_results/")
    args = parser.parse_args()

    local_res = run_backtest(
        date_from=args.date_from,
        date_to=args.date_to,
        model=args.model,
        save_results=False,
        fast=False,
        data_source="local",
        local_index=args.local_index,
    )
    nba_res = run_backtest(
        date_from=args.date_from,
        date_to=args.date_to,
        model=args.model,
        save_results=False,
        fast=False,
        data_source="nba",
    )

    summary = {
        "success": bool(local_res.get("success") and nba_res.get("success")),
        "dateFrom": args.date_from,
        "dateTo": args.date_to,
        "model": args.model,
        "localSuccess": bool(local_res.get("success")),
        "nbaSuccess": bool(nba_res.get("success")),
        "localError": local_res.get("error"),
        "nbaError": nba_res.get("error"),
        "localIndexPath": local_res.get("localIndexPath"),
        "comparison": {},
    }

    if summary["success"]:
        for m in _model_names(args.model):
            l_rep = (local_res.get("reports") or {}).get(m, {})
            n_rep = (nba_res.get("reports") or {}).get(m, {})
            l_roi = l_rep.get("roiSimulation") or {}
            n_roi = n_rep.get("roiSimulation") or {}
            summary["comparison"][m] = {
                "sampleCountLocal": l_rep.get("sampleCount"),
                "sampleCountNba": n_rep.get("sampleCount"),
                "sampleDeltaLocalMinusNba": None
                if l_rep.get("sampleCount") is None or n_rep.get("sampleCount") is None
                else int(l_rep.get("sampleCount")) - int(n_rep.get("sampleCount")),
                "projectionErrorsLocal": l_rep.get("projectionErrors"),
                "projectionErrorsNba": n_rep.get("projectionErrors"),
                "roiPctPerBetLocal": l_roi.get("roiPctPerBet"),
                "roiPctPerBetNba": n_roi.get("roiPctPerBet"),
                "hitRatePctLocal": l_roi.get("hitRatePct"),
                "hitRatePctNba": n_roi.get("hitRatePct"),
                "byStat": _report_delta(l_rep, n_rep),
            }

    if args.save:
        out_dir = Path(__file__).resolve().parents[1] / "data" / "backtest_results"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"parity_{args.date_from}_to_{args.date_to}_{args.model}.json"
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        summary["savedTo"] = str(out_path)

    print(json.dumps(summary, indent=2))
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
