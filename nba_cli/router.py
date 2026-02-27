#!/usr/bin/env python3
"""CLI command routing."""

from .core_commands import handle_core_command
from .ev_commands import handle_ev_command
from .line_commands import handle_line_command
from .llm_commands import handle_llm_command
from .ml_commands import handle_ml_command
from .shared import no_command_payload
from .tracking_commands import handle_tracking_command

_HANDLERS = (
    handle_core_command,
    handle_ev_command,
    handle_line_command,
    handle_llm_command,
    handle_ml_command,
    handle_tracking_command,
)


def dispatch_cli(argv):
    if len(argv) < 2:
        return no_command_payload(), 1

    command = argv[1]
    for handler in _HANDLERS:
        payload = handler(command, argv)
        if payload is not None:
            return payload, 0

    return {"error": f"Unknown command: {command}"}, 0
