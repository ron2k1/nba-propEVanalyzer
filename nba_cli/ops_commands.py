#!/usr/bin/env python3
"""Ops commands: daily_ops — collect_lines -> roster_sweep -> best_today."""

from core.pipeline_progress import merge_status, read_status


def _parse_daily_ops_args(argv):
    progress_file = None
    idx = 2
    while idx < len(argv):
        token = str(argv[idx]).strip()
        if token == "--progress-file" and idx + 1 < len(argv):
            progress_file = str(argv[idx + 1]).strip()
            idx += 2
            continue
        idx += 1
    return {
        "dry_run": "--dry-run" in argv,
        "progress_file": progress_file,
    }


def _default_daily_ops_steps():
    return [
        {"name": "Collect Lines", "status": "pending", "result": None},
        {"name": "Roster Sweep", "status": "pending", "result": None},
        {"name": "Best Today", "status": "pending", "result": None},
    ]


def _write_daily_ops_progress(progress_file, *, stage, message, steps, step_index=None, completed=False):
    existing = read_status(progress_file)
    merge_status(
        progress_file,
        taskName=existing.get("taskName") or "daily_ops",
        currentCommand="daily_ops",
        busy=not completed,
        stage=stage,
        message=message,
        stepIndex=step_index,
        totalSteps=len(steps),
        steps=steps,
    )


def _handle_daily_ops(argv):
    """
    daily_ops [--dry-run]

    Runs the daily prop pipeline in sequence:
      1. collect_lines (betmgm, draftkings, fanduel; pts, ast, pra)
      2. roster_sweep  (journals qualifying signals from today's snapshots)
      3. best_today    (top 20 signals from the decision journal)

    --dry-run: runs collect_lines but skips roster_sweep journal writes.
    """
    parsed = _parse_daily_ops_args(argv)
    dry_run = parsed["dry_run"]
    progress_file = parsed["progress_file"]

    results = {"success": False, "steps": {}}
    ui_steps = _default_daily_ops_steps()

    _write_daily_ops_progress(
        progress_file,
        stage="collect_lines",
        message="Collecting latest sportsbook lines.",
        steps=ui_steps,
        step_index=1,
    )

    # Step 1: collect_lines — must succeed for downstream steps to be useful
    try:
        from nba_cli.line_commands import _COMMANDS as _LINE_CMDS
        collect_argv = [
            "nba_mod.py", "collect_lines",
            "--books", "betmgm,draftkings,fanduel",
            "--stats", "pts,ast,pra",
        ]
        collect_result = _LINE_CMDS["collect_lines"](collect_argv)
        results["steps"]["collect_lines"] = {
            "success": collect_result.get("success", False),
            "snapshotCount": collect_result.get("snapshotCount", 0),
            "error": collect_result.get("error"),
        }
        ui_steps[0]["status"] = "done" if collect_result.get("success") else "error"
        ui_steps[0]["result"] = results["steps"]["collect_lines"]
    except Exception as ex:
        results["steps"]["collect_lines"] = {"success": False, "error": str(ex)}
        ui_steps[0]["status"] = "error"
        ui_steps[0]["result"] = results["steps"]["collect_lines"]

    _write_daily_ops_progress(
        progress_file,
        stage="roster_sweep" if not dry_run else "best_today",
        message="Running roster sweep against stored snapshots." if not dry_run else "Dry run: skipping roster sweep.",
        steps=ui_steps,
        step_index=2 if not dry_run else 3,
        completed=False,
    )

    # Step 2: roster_sweep (skip if dry_run)
    if not dry_run:
        try:
            from nba_cli.scan_commands import _COMMANDS as _SCAN_CMDS
            sweep_argv = ["nba_mod.py", "roster_sweep"]
            if progress_file:
                sweep_argv.extend(["--progress-file", progress_file])
            sweep_result = _SCAN_CMDS["roster_sweep"](sweep_argv)
            results["steps"]["roster_sweep"] = {
                "success": sweep_result.get("success", False),
                "scanned": sweep_result.get("scanned", 0),
                "logged": sweep_result.get("logged", 0),
                "top5": sweep_result.get("top5", []),
                "snapshotOnly": sweep_result.get("snapshotOnly", True),
            }
            ui_steps[1]["status"] = "done" if sweep_result.get("success") else "error"
            ui_steps[1]["result"] = results["steps"]["roster_sweep"]
        except Exception as ex:
            results["steps"]["roster_sweep"] = {"success": False, "error": str(ex)}
            ui_steps[1]["status"] = "error"
            ui_steps[1]["result"] = results["steps"]["roster_sweep"]
    else:
        results["steps"]["roster_sweep"] = {"skipped": True, "reason": "dry_run"}
        ui_steps[1]["status"] = "done"
        ui_steps[1]["result"] = results["steps"]["roster_sweep"]

    _write_daily_ops_progress(
        progress_file,
        stage="best_today",
        message="Refreshing best-today journal summary.",
        steps=ui_steps,
        step_index=3,
    )

    # Step 3: best_today (top 20 signals)
    try:
        from nba_cli.tracking_commands import _COMMANDS as _TRACKING_CMDS
        best_argv = ["nba_mod.py", "best_today", "20"]
        best_fn = _TRACKING_CMDS.get("best_today")
        if best_fn:
            r = best_fn(best_argv)
            results["steps"]["best_today"] = {
                "success": r.get("success", False),
                "count": len(r.get("signals", r.get("bets", []))),
            }
            ui_steps[2]["status"] = "done" if r.get("success", False) else "error"
            ui_steps[2]["result"] = results["steps"]["best_today"]
        else:
            results["steps"]["best_today"] = {
                "skipped": True,
                "reason": "best_today_not_registered",
            }
            ui_steps[2]["status"] = "done"
            ui_steps[2]["result"] = results["steps"]["best_today"]
    except Exception as ex:
        results["steps"]["best_today"] = {"success": False, "error": str(ex)}
        ui_steps[2]["status"] = "error"
        ui_steps[2]["result"] = results["steps"]["best_today"]

    results["dryRun"] = dry_run
    # Top-level success requires collect_lines (feeds everything downstream)
    results["success"] = results["steps"].get("collect_lines", {}).get("success", False)

    _write_daily_ops_progress(
        progress_file,
        stage="completed" if results["success"] else "failed",
        message="Daily pipeline finished." if results["success"] else "Daily pipeline finished with errors.",
        steps=ui_steps,
        step_index=3,
        completed=True,
    )
    return results


_COMMANDS = {
    "daily_ops": _handle_daily_ops,
}
