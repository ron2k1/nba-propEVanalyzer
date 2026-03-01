#!/usr/bin/env python3
"""
Generate train vs validation overfitting chart from backtest results.

Splits date range: train = first 70%, val = last 30%.
Runs two real-line-only backtests, then produces an HTML chart.

Usage:
    python scripts/overfitting_chart.py [--date-from 2026-01-26] [--date-to 2026-02-25]
    # Output: docs/reports/overfitting_chart.html
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "data" / "backtest_results"
OUTPUT_HTML = ROOT / "docs" / "reports" / "overfitting_chart.html"


def _run_backtest(date_from: str, date_to: str) -> dict | None:
    """Run real-line-only backtest; return parsed JSON or None."""
    cmd = [
        sys.executable,
        str(ROOT / "nba_mod.py"),
        "backtest",
        date_from,
        date_to,
        "--model", "full",
        "--local",
        "--odds-source", "local_history",
        "--real-only",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return None
        data = json.loads(lines[-1])
        if data.get("success") and "reports" in data:
            return data
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"  Backtest error: {e}", file=sys.stderr)
        return None


def _extract_metrics(data: dict) -> dict:
    """Extract brier, hit rate, roi from backtest report."""
    r = data.get("reports", {}).get("full", {})
    roi = r.get("roiReal", {}) or {}
    brier = r.get("brierByStat", {}) or {}
    cal = r.get("calibrationByStat", {}) or {}

    # Flatten calibration for reliability diagram (use pts as representative)
    pts_cal = cal.get("pts", []) or []
    pred_actual = [
        (b["avgPredOverProbPct"], b["actualOverHitRatePct"], b.get("count", 0))
        for b in pts_cal
        if b.get("avgPredOverProbPct") is not None and b.get("actualOverHitRatePct") is not None and b.get("count", 0) >= 5
    ]

    return {
        "brier": {k: v for k, v in brier.items() if v is not None},
        "hitRatePct": roi.get("hitRatePct"),
        "roiPctPerBet": roi.get("roiPctPerBet"),
        "betsPlaced": roi.get("betsPlaced"),
        "sampleCount": r.get("sampleCount"),
        "calPredActual": pred_actual,
    }


def _build_html(train_metrics: dict, val_metrics: dict, train_range: str, val_range: str) -> str:
    stats = sorted(set(list(train_metrics.get("brier", {}).keys()) + list(val_metrics.get("brier", {}).keys())))
    train_brier = [train_metrics.get("brier", {}).get(s) for s in stats]
    val_brier = [val_metrics.get("brier", {}).get(s) for s in stats]
    train_brier = [v if v is not None else 0 for v in train_brier]
    val_brier = [v if v is not None else 0 for v in val_brier]

    cal_data = val_metrics.get("calPredActual", []) or train_metrics.get("calPredActual", [])
    pred_vals = [p[0] for p in cal_data]
    actual_vals = [p[1] for p in cal_data]
    counts = [p[2] for p in cal_data]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Train vs Validation — Overfitting Check</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; background: #1a1a1a; color: #e0e0e0; }}
    h1 {{ font-size: 1.25rem; margin-bottom: 8px; }}
    .meta {{ font-size: 0.85rem; color: #888; margin-bottom: 24px; }}
    .chart-row {{ display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 32px; }}
    .chart-wrap {{ width: min(450px, 100%); }}
    .chart-wrap h2 {{ font-size: 1rem; margin-bottom: 8px; }}
    canvas {{ max-height: 280px; }}
  </style>
</head>
<body>
  <h1>Train vs Validation — Model Overfitting Check</h1>
  <div class="meta">
    Train: {train_range} | Validation: {val_range} (ROI from real lines)
  </div>
  <div class="chart-row">
    <div class="chart-wrap">
      <h2>Brier Score by Stat (lower = better)</h2>
      <canvas id="brierChart"></canvas>
    </div>
    <div class="chart-wrap">
      <h2>Hit Rate %</h2>
      <canvas id="hitChart"></canvas>
    </div>
    <div class="chart-wrap">
      <h2>ROI % per bet</h2>
      <canvas id="roiChart"></canvas>
    </div>
  </div>
  <div class="chart-row">
    <div class="chart-wrap">
      <h2>Calibration: Predicted vs Actual (pts bins)</h2>
      <canvas id="calChart"></canvas>
    </div>
  </div>
  <script>
    const stats = {json.dumps(stats)};
    const trainBrier = {json.dumps(train_brier)};
    const valBrier = {json.dumps(val_brier)};
    const predVals = {json.dumps(pred_vals)};
    const actualVals = {json.dumps(actual_vals)};
    const trainHit = {json.dumps(train_metrics.get("hitRatePct"))};
    const valHit = {json.dumps(val_metrics.get("hitRatePct"))};
    const trainRoi = {json.dumps(train_metrics.get("roiPctPerBet"))};
    const valRoi = {json.dumps(val_metrics.get("roiPctPerBet"))};

    Chart.defaults.color = '#aaa';
    Chart.defaults.borderColor = '#333';

    new Chart(document.getElementById('brierChart'), {{
      type: 'bar',
      data: {{
        labels: stats,
        datasets: [
          {{ label: 'Train', data: trainBrier, backgroundColor: 'rgba(59, 130, 246, 0.7)' }},
          {{ label: 'Validation', data: valBrier, backgroundColor: 'rgba(34, 197, 94, 0.7)' }}
        ]
      }},
      options: {{
        plugins: {{ legend: {{ position: 'top' }} }},
        scales: {{
          y: {{ beginAtZero: true, title: {{ display: true, text: 'Brier' }} }},
          x: {{ title: {{ display: true, text: 'Stat' }} }}
        }}
      }}
    }});

    new Chart(document.getElementById('hitChart'), {{
      type: 'bar',
      data: {{
        labels: ['Train', 'Validation'],
        datasets: [{{ label: 'Hit Rate %', data: [trainHit || 0, valHit || 0], backgroundColor: ['rgba(59, 130, 246, 0.7)', 'rgba(34, 197, 94, 0.7)'] }}]
      }},
      options: {{
        plugins: {{ legend: {{ display: false }} }},
        scales: {{ y: {{ beginAtZero: true, max: 100 }} }}
      }}
    }});
    new Chart(document.getElementById('roiChart'), {{
      type: 'bar',
      data: {{
        labels: ['Train', 'Validation'],
        datasets: [{{ label: 'ROI % per bet', data: [trainRoi || 0, valRoi || 0], backgroundColor: ['rgba(59, 130, 246, 0.7)', 'rgba(34, 197, 94, 0.7)'] }}]
      }},
      options: {{
        plugins: {{ legend: {{ display: false }} }},
        scales: {{ y: {{ beginAtZero: true }} }}
      }}
    }});

    const diagData = predVals.map((p, i) => ({{ x: p, y: actualVals[i] }}));
    const perfectLine = [0,25,50,75,100].map(v => ({{ x: v, y: v }}));
    new Chart(document.getElementById('calChart'), {{
      type: 'scatter',
      data: {{
        datasets: [
          {{ label: 'Actual vs Predicted', data: diagData, backgroundColor: 'rgba(34, 197, 94, 0.6)', pointRadius: 8 }},
          {{ label: 'Perfect calibration (y=x)', data: perfectLine, type: 'line', borderColor: 'rgba(248, 250, 252, 0.6)', borderWidth: 2, pointRadius: 0, fill: false }}
        ]
      }},
      options: {{
        plugins: {{ legend: {{ position: 'top' }} }},
        scales: {{
          x: {{ min: 0, max: 100, title: {{ display: true, text: 'Predicted prob %' }} }},
          y: {{ min: 0, max: 100, title: {{ display: true, text: 'Actual hit rate %' }} }}
        }}
      }}
    }});
  </script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="Train vs Validation overfitting chart")
    ap.add_argument("--date-from", default="2026-01-26", help="Start date")
    ap.add_argument("--date-to", default="2026-02-25", help="End date (exclusive of today)")
    ap.add_argument("--train-frac", type=float, default=0.70, help="Fraction for train split")
    ap.add_argument("--train-json", help="Use existing train backtest JSON (skip run)")
    ap.add_argument("--val-json", help="Use existing validation backtest JSON (skip run)")
    args = ap.parse_args()

    from_dt = datetime.strptime(args.date_from, "%Y-%m-%d").date()
    to_dt = datetime.strptime(args.date_to, "%Y-%m-%d").date()
    n_days = (to_dt - from_dt).days + 1
    n_train = max(1, int(n_days * args.train_frac))
    split_dt = from_dt + timedelta(days=n_train - 1)
    train_to = split_dt.isoformat()
    val_from = (split_dt + timedelta(days=1)).isoformat()

    train_range = f"{args.date_from} → {train_to}"
    val_range = f"{val_from} → {args.date_to}"

    if args.train_json and args.val_json:
        with open(args.train_json, encoding="utf-8") as f:
            train_data = json.load(f)
        with open(args.val_json, encoding="utf-8") as f:
            val_data = json.load(f)
        train_range = f"{train_data.get('dateFrom', '?')} → {train_data.get('dateTo', '?')}"
        val_range = f"{val_data.get('dateFrom', '?')} → {val_data.get('dateTo', '?')}"
        print("Using provided JSON files.", file=sys.stderr, flush=True)
    else:
        print("Running train backtest...", file=sys.stderr, flush=True)
        train_data = _run_backtest(args.date_from, train_to)
        if not train_data:
            print("Train backtest failed.", file=sys.stderr)
            sys.exit(1)

        print("Running validation backtest...", file=sys.stderr, flush=True)
        val_data = _run_backtest(val_from, args.date_to)
        if not val_data:
            print("Validation backtest failed.", file=sys.stderr)
            sys.exit(1)

    print(f"Train: {train_range}", file=sys.stderr, flush=True)
    print(f"Val:   {val_range}", file=sys.stderr, flush=True)

    train_metrics = _extract_metrics(train_data)
    val_metrics = _extract_metrics(val_data)

    html = _build_html(train_metrics, val_metrics, train_range, val_range)

    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"Wrote: {OUTPUT_HTML}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
