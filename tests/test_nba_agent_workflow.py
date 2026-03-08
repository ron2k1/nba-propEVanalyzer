import importlib.util
from pathlib import Path


def _load_nba_agent_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "scripts" / "nba_agent.py"
    spec = importlib.util.spec_from_file_location("scripts_nba_agent_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_daily_scan_workflow_order_and_timeout():
    module = _load_nba_agent_module()
    steps = module.WORKFLOWS["daily_scan"]["steps"]

    assert [step["name"] for step in steps] == [
        "collect_lines",
        "roster_sweep",
        "best_today",
    ]
    assert steps[1]["timeout_sec"] == module._STEP_TIMEOUT_LONG


def test_line_collect_workflow_contains_only_collect_lines():
    module = _load_nba_agent_module()
    steps = module.WORKFLOWS["line_collect"]["steps"]

    assert [step["name"] for step in steps] == ["collect_lines"]


def test_full_pipeline_workflow_sequence():
    module = _load_nba_agent_module()
    steps = module.WORKFLOWS["full_pipeline"]["steps"]

    assert [step["name"] for step in steps] == [
        "collect_lines",
        "roster_sweep",
        "best_today",
        "paper_settle",
        "paper_summary",
        "journal_gate",
    ]
    assert steps[1]["timeout_sec"] == module._STEP_TIMEOUT_LONG


def test_daily_scan_dry_run_reports_step_timeouts():
    module = _load_nba_agent_module()
    ctx = {
        "today": "2026-03-07",
        "yesterday": "2026-03-06",
        "date_from": "2026-02-28",
        "date_to": "2026-03-06",
    }

    result = module.run_workflow("daily_scan", ctx, dry_run=True, verbose=False)

    assert [step["step"] for step in result["steps"]] == [
        "collect_lines",
        "roster_sweep",
        "best_today",
    ]
    assert [step["timeoutSec"] for step in result["steps"]] == [
        module._STEP_TIMEOUT_DEFAULT,
        module._STEP_TIMEOUT_LONG,
        module._STEP_TIMEOUT_DEFAULT,
    ]
