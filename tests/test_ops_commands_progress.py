import json
import sys
import types
import uuid
from pathlib import Path

from nba_cli import ops_commands


def test_daily_ops_writes_step_progress(monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    tmp_dir = repo_root / "data" / "logs"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    progress_path = tmp_dir / f"pipeline_status_{uuid.uuid4().hex}.json"

    fake_line_commands = types.ModuleType("nba_cli.line_commands")
    fake_line_commands._COMMANDS = {
        "collect_lines": lambda argv: {
            "success": True,
            "snapshotCount": 42,
            "error": None,
        }
    }
    fake_scan_commands = types.ModuleType("nba_cli.scan_commands")
    fake_scan_commands._COMMANDS = {
        "roster_sweep": lambda argv: {
            "success": True,
            "scanned": 15,
            "logged": 2,
            "top5": [],
            "snapshotOnly": True,
        }
    }
    fake_tracking_commands = types.ModuleType("nba_cli.tracking_commands")
    fake_tracking_commands._COMMANDS = {
        "best_today": lambda argv: {
            "success": True,
            "signals": [{"playerName": "Test Player"}],
        }
    }

    monkeypatch.setitem(sys.modules, "nba_cli.line_commands", fake_line_commands)
    monkeypatch.setitem(sys.modules, "nba_cli.scan_commands", fake_scan_commands)
    monkeypatch.setitem(sys.modules, "nba_cli.tracking_commands", fake_tracking_commands)

    result = ops_commands._handle_daily_ops(
        [
            "nba_mod.py",
            "daily_ops",
            "--progress-file",
            str(progress_path),
        ]
    )

    status = json.loads(progress_path.read_text(encoding="utf-8"))

    assert result["success"] is True
    assert status["busy"] is False
    assert status["stage"] == "completed"
    assert [step["name"] for step in status["steps"]] == [
        "Collect Lines",
        "Roster Sweep",
        "Best Today",
    ]
    assert status["steps"][0]["status"] == "done"
    assert status["steps"][1]["result"]["snapshotOnly"] is True
    assert status["steps"][2]["result"]["count"] == 1
