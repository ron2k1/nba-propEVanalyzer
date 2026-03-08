#!/usr/bin/env python3
"""
nba_agent.py — Fixed-workflow runner with GPT-OSS summarization.

Each workflow is a fixed sequence of nba_mod.py commands. All outputs are
collected as JSON, then passed to GPT-OSS (Ollama) for a plain-English summary
that is returned as the final structured payload.

Usage:
    python scripts/nba_agent.py --workflow daily_scan
    python scripts/nba_agent.py --workflow morning_settle [--date YYYY-MM-DD]
    python scripts/nba_agent.py --workflow backtest_quick --date-from 2026-02-19 --date-to 2026-02-25
    python scripts/nba_agent.py --list

Output (stdout, last line): JSON with keys:
    workflow, steps[], summary (GPT-OSS plain-English), provider, ok
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date, timedelta

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
MOD = os.path.join(ROOT, "nba_mod.py")

_OLLAMA_BASE = "http://localhost:11434"
_OLLAMA_MODEL = "gpt-oss:20b"
_OLLAMA_TIMEOUT = 120
_STEP_TIMEOUT_DEFAULT = 120
_STEP_TIMEOUT_LONG = 600


# ── Workflow definitions ──────────────────────────────────────────────────────

WORKFLOWS = {
    "daily_scan": {
        "description": "Collect lines → roster_sweep → best_today → log signals",
        "steps": [
            {
                "name": "collect_lines",
                "args": ["collect_lines", "--books", "betmgm,draftkings,fanduel", "--stats", "pts,ast"],
                "optional": True,
            },
            {
                "name": "roster_sweep",
                "args": ["roster_sweep"],
                "timeout_sec": _STEP_TIMEOUT_LONG,
            },
            {
                "name": "best_today",
                "args": ["best_today", "20"],
            },
        ],
    },
    "morning_settle": {
        "description": "Settle yesterday → results → 14-day paper summary",
        "steps": [
            {
                "name": "paper_settle",
                "args": ["paper_settle", "{yesterday}"],
            },
            {
                "name": "results_yesterday",
                "args": ["results_yesterday", "50"],
            },
            {
                "name": "paper_summary",
                "args": ["paper_summary", "--window-days", "14"],
            },
        ],
    },
    "backtest_quick": {
        "description": "7-day real-line backtest (date-from / date-to required)",
        "steps": [
            {
                "name": "backtest",
                "args": ["backtest", "{date_from}", "{date_to}",
                         "--model", "full", "--local", "--odds-source", "local_history", "--save"],
            },
        ],
    },
    "weekly_report": {
        "description": "30-day backtest + 14-day paper summary side-by-side",
        "steps": [
            {
                "name": "paper_summary",
                "args": ["paper_summary", "--window-days", "30"],
            },
            {
                "name": "journal_gate",
                "args": ["journal_gate"],
            },
        ],
    },
}


# ── Command runner ────────────────────────────────────────────────────────────

def _run_step(args, dry_run=False, timeout_sec=_STEP_TIMEOUT_DEFAULT):
    """Run a single nba_mod.py command, return (result_dict, raw_stdout, error)."""
    cmd = [PYTHON, MOD] + args
    if dry_run:
        return {"dry_run": True, "cmd": " ".join(args)}, "", None

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=ROOT,
        )
        stdout = proc.stdout.strip()
        if not stdout:
            return None, "", proc.stderr.strip() or "no output"

        # Last line is the parseable JSON payload (per CLAUDE.md convention)
        last_line = stdout.splitlines()[-1]
        try:
            result = json.loads(last_line)
            return result, stdout, None
        except json.JSONDecodeError:
            return {"raw": stdout}, stdout, None

    except subprocess.TimeoutExpired:
        return None, "", f"timeout after {timeout_sec}s"
    except Exception as exc:
        return None, "", str(exc)


def _interpolate(args, ctx):
    """Replace {placeholder} tokens in arg list with values from ctx dict."""
    out = []
    for a in args:
        for k, v in ctx.items():
            a = a.replace("{" + k + "}", str(v))
        out.append(a)
    return out


# ── GPT-OSS summarization ─────────────────────────────────────────────────────

def _trim_for_summary(step_results):
    """Build a compact summary dict — GPT-OSS doesn't need full arrays."""
    out = []
    for s in step_results:
        r = s.get("result") or {}
        name = s["step"]
        if name == "best_today":
            out.append({
                "step": name,
                "ok": s["ok"],
                "totalRanked": r.get("totalRanked"),
                "positiveEdgeCount": r.get("positiveEdgeCount"),
                "policyQualified": [
                    {k: v for k, v in e.items()
                     if k in ("playerName", "stat", "line", "recommendedSide",
                               "recommendedEvPct", "recommendedOdds", "projection",
                               "lineMovementConflict")}
                    for e in (r.get("policyQualified") or [])
                ],
                "top5": [
                    {k: v for k, v in e.items()
                     if k in ("playerName", "stat", "line", "recommendedSide",
                               "recommendedEvPct", "recommendedOdds", "projection",
                               "policyQualified", "lineMovementConflict")}
                    for e in (r.get("topOffers") or [])[:5]
                ],
            })
        elif name == "roster_sweep":
            out.append({
                "step": name,
                "ok": s["ok"],
                "scanned": r.get("scanned"),
                "logged": r.get("logged"),
                "skipped": r.get("skipped"),
                "top5": r.get("top5", []),
            })
        elif name == "collect_lines":
            out.append({
                "step": name,
                "ok": s["ok"],
                "eventCount": r.get("eventCount"),
                "snapshotCount": r.get("snapshotCount"),
            })
        else:
            # Generic: drop large arrays, keep top-level scalars
            out.append({
                "step": name,
                "ok": s["ok"],
                "result": {k: v for k, v in r.items()
                           if not isinstance(v, (list, dict))},
            })
    return out


def _summarize(workflow_name, step_results, dry_run=False):
    """Send collected step results to GPT-OSS and return a plain-English summary."""
    if dry_run:
        return "dry-run mode — no LLM call", "dry-run"

    payload = json.dumps(
        {"workflow": workflow_name, "steps": _trim_for_summary(step_results)},
        indent=2,
        default=str,
    )

    system = (
        "You are an NBA prop betting analyst assistant. "
        "You receive structured JSON output from an automated pipeline and write a concise, "
        "actionable plain-English summary for the trader. "
        "Focus on: top value bets (edge ≥ 8%, bins 0-10% or 10-20%), "
        "signal count, any warnings, and overall verdict (GO / NO-GO / WATCH). "
        "Be direct. No fluff. Use bullet points."
    )
    user = (
        f"Summarize the following workflow results for workflow '{workflow_name}':\n\n"
        f"{payload}\n\n"
        "Return a clear, actionable summary with: top signals, key stats, and a one-line verdict."
    )

    try:
        resp = requests.post(
            f"{_OLLAMA_BASE}/api/chat",
            json={
                "model": _OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=_OLLAMA_TIMEOUT,
        )
        if resp.status_code != 200:
            return f"Ollama error {resp.status_code}", "ollama_error"
        content = (resp.json().get("message") or {}).get("content", "")
        return content.strip(), _OLLAMA_MODEL
    except requests.exceptions.ConnectionError:
        return "Ollama not running — summary unavailable", "unavailable"
    except Exception as exc:
        return f"Summary error: {exc}", "error"


# ── Main ──────────────────────────────────────────────────────────────────────

def run_workflow(workflow_name, ctx, dry_run=False, verbose=False):
    wf = WORKFLOWS[workflow_name]
    step_defs = wf["steps"]

    step_results = []
    all_ok = True

    for step_def in step_defs:
        name = step_def["name"]
        args = _interpolate(step_def["args"], ctx)
        optional = step_def.get("optional", False)
        timeout_sec = int(step_def.get("timeout_sec", _STEP_TIMEOUT_DEFAULT))

        if verbose:
            print(f"  → {name}: {' '.join(args)}", file=sys.stderr)

        result, raw, err = _run_step(args, dry_run=dry_run, timeout_sec=timeout_sec)

        step_entry = {
            "step": name,
            "args": args,
            "timeoutSec": timeout_sec,
            "ok": err is None and result is not None,
            "result": result,
            "error": err,
        }
        step_results.append(step_entry)

        if err and not optional:
            all_ok = False
            if verbose:
                print(f"    ✗ {name}: {err}", file=sys.stderr)
        elif verbose:
            print(f"    ✓ {name}", file=sys.stderr)

    summary, provider = _summarize(workflow_name, step_results, dry_run=dry_run)

    return {
        "ok": all_ok,
        "workflow": workflow_name,
        "description": wf["description"],
        "steps": step_results,
        "summary": summary,
        "provider": provider,
    }


def main():
    parser = argparse.ArgumentParser(description="NBA workflow agent with GPT-OSS summarization")
    parser.add_argument("--workflow", "-w", help="Workflow to run")
    parser.add_argument("--list", action="store_true", help="List available workflows")
    parser.add_argument("--date", help="Override date (YYYY-MM-DD) for workflows that need it")
    parser.add_argument("--date-from", dest="date_from", help="Backtest start date")
    parser.add_argument("--date-to", dest="date_to", help="Backtest end date")
    parser.add_argument("--dry-run", action="store_true", help="Show commands without executing")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print step progress to stderr")
    args = parser.parse_args()

    if args.list:
        print(json.dumps({
            "workflows": {k: v["description"] for k, v in WORKFLOWS.items()}
        }, indent=2))
        return

    if not args.workflow:
        parser.error("--workflow is required (or use --list)")

    if args.workflow not in WORKFLOWS:
        print(json.dumps({
            "ok": False,
            "error": f"Unknown workflow '{args.workflow}'",
            "available": list(WORKFLOWS.keys()),
        }))
        sys.exit(1)

    today = date.today()
    yesterday = today - timedelta(days=1)

    ctx = {
        "yesterday": yesterday.isoformat(),
        "today": today.isoformat(),
        "date_from": args.date_from or (today - timedelta(days=7)).isoformat(),
        "date_to": args.date_to or yesterday.isoformat(),
    }
    if args.date:
        ctx["yesterday"] = args.date

    output = run_workflow(args.workflow, ctx, dry_run=args.dry_run, verbose=args.verbose)
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
