#!/usr/bin/env python3
"""
NBA Pipeline CLI entrypoint.

Command handlers live under `nba_cli/` by domain:
  - core_commands.py
  - ev_commands.py
  - tracking_commands.py
  - ml_commands.py
"""

import json
import sys
import traceback

from dotenv import load_dotenv

load_dotenv(override=True)

from nba_cli import dispatch_cli


if __name__ == "__main__":
    try:
        result, exit_code = dispatch_cli(sys.argv)
        print(json.dumps(result, default=str))
        if exit_code:
            sys.exit(exit_code)
    except Exception as e:
        print(json.dumps({"error": str(e), "traceback": traceback.format_exc()}))
        sys.exit(1)
