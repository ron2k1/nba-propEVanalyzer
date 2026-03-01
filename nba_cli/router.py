#!/usr/bin/env python3
"""CLI command routing — O(1) registry-based dispatch."""

from .backtest_commands   import _COMMANDS as _BACKTEST
from .core_commands       import _COMMANDS as _CORE
from .ev_commands         import _COMMANDS as _EV
from .journal_commands    import _COMMANDS as _JOURNAL
from .line_commands       import _COMMANDS as _LINE
from .llm_commands        import _COMMANDS as _LLM
from .ml_commands         import _COMMANDS as _ML
from .odds_commands       import _COMMANDS as _ODDS
from .ops_commands        import _COMMANDS as _OPS
from .projection_commands import _COMMANDS as _PROJ
from .scan_commands       import _COMMANDS as _SCAN
from .tracking_commands   import _COMMANDS as _TRACKING
from .shared              import no_command_payload

_REGISTRY = {
    **_CORE, **_EV, **_PROJ, **_BACKTEST,
    **_LINE, **_ODDS, **_LLM, **_ML, **_TRACKING, **_JOURNAL,
    **_SCAN, **_OPS,
}


def dispatch_cli(argv):
    if len(argv) < 2:
        return no_command_payload(), 1
    handler = _REGISTRY.get(argv[1])
    if handler is None:
        return {"error": f"Unknown command: {argv[1]}"}, 0
    return handler(argv), 0
