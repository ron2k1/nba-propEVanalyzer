#!/usr/bin/env python3
"""Ops commands: daily_ops — collect_lines -> roster_sweep -> best_today."""


def _handle_daily_ops(argv):
    """
    daily_ops [--dry-run]

    Runs the daily prop pipeline in sequence:
      1. collect_lines (betmgm, draftkings, fanduel; pts, ast, pra)
      2. roster_sweep  (journals qualifying signals from today's snapshots)
      3. best_today    (top 20 signals from the decision journal)

    --dry-run: runs collect_lines but skips roster_sweep journal writes.
    """
    dry_run = "--dry-run" in argv

    results = {"success": True, "steps": {}}

    # Step 1: collect_lines
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
            "snapshotsWritten": collect_result.get("snapshotsWritten", 0),
            "error": collect_result.get("error"),
        }
    except Exception as ex:
        results["steps"]["collect_lines"] = {"success": False, "error": str(ex)}

    # Step 2: roster_sweep (skip if dry_run)
    if not dry_run:
        try:
            from nba_cli.scan_commands import _COMMANDS as _SCAN_CMDS
            sweep_argv = ["nba_mod.py", "roster_sweep"]
            sweep_result = _SCAN_CMDS["roster_sweep"](sweep_argv)
            results["steps"]["roster_sweep"] = {
                "success": sweep_result.get("success", False),
                "scanned": sweep_result.get("scanned", 0),
                "logged": sweep_result.get("logged", 0),
                "top5": sweep_result.get("top5", []),
            }
        except Exception as ex:
            results["steps"]["roster_sweep"] = {"success": False, "error": str(ex)}
    else:
        results["steps"]["roster_sweep"] = {"skipped": True, "reason": "dry_run"}

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
        else:
            results["steps"]["best_today"] = {
                "skipped": True,
                "reason": "best_today_not_registered",
            }
    except Exception as ex:
        results["steps"]["best_today"] = {"success": False, "error": str(ex)}

    results["dryRun"] = dry_run
    return results


_COMMANDS = {
    "daily_ops": _handle_daily_ops,
}
