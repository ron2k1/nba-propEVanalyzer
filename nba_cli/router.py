#!/usr/bin/env python3
"""CLI command routing — O(1) registry-based dispatch."""

import logging
import os
from pathlib import Path

from .backtest_commands    import _COMMANDS as _BACKTEST
from .core_commands        import _COMMANDS as _CORE
from .ev_commands          import _COMMANDS as _EV
from .journal_commands     import _COMMANDS as _JOURNAL
from .line_commands        import _COMMANDS as _LINE
from .llm_commands         import _COMMANDS as _LLM
from .manual_bet_commands  import _COMMANDS as _MANUAL
from .ml_commands          import _COMMANDS as _ML
from .odds_commands        import _COMMANDS as _ODDS
from .ops_commands         import _COMMANDS as _OPS
from .projection_commands  import _COMMANDS as _PROJ
from .scan_commands        import _COMMANDS as _SCAN
from .tracking_commands    import _COMMANDS as _TRACKING
from .lightrag_commands    import _COMMANDS as _LIGHTRAG
from .shared               import no_command_payload

_REGISTRY = {
    **_CORE, **_EV, **_PROJ, **_BACKTEST,
    **_LINE, **_ODDS, **_LLM, **_ML, **_TRACKING, **_JOURNAL,
    **_SCAN, **_OPS, **_MANUAL, **_LIGHTRAG,
}


def _setup_cli_logging():
    """Configure file + console logging for CLI mode (mirrors server.py).

    Default level is WARNING so CLI stays quiet.  When --verbose escalates the
    nba_engine logger to DEBUG, the file handler sees those messages too because
    Python filters at the *logger* level (the gate), not the handler level
    (which defaults to NOTSET = pass-through).
    """
    root = Path(__file__).resolve().parent.parent
    log_dir = root / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, os.getenv("NBA_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
    logger = logging.getLogger("nba_engine")
    if not logger.handlers:  # avoid duplicate handlers on re-import
        logger.setLevel(level)
        fh = logging.FileHandler(str(log_dir / "pipeline.log"), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logger.addHandler(fh)


def dispatch_cli(argv):
    _setup_cli_logging()
    if len(argv) < 2:
        return no_command_payload(), 1
    handler = _REGISTRY.get(argv[1])
    if handler is None:
        return {"error": f"Unknown command: {argv[1]}"}, 0
    return handler(argv), 0
