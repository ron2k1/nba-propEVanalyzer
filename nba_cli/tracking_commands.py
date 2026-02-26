#!/usr/bin/env python3
"""Bet tracking and settlement CLI commands."""

import json

from nba_bet_tracking import (
    best_plays_for_date,
    best_today,
    export_training_rows,
    record_closing_values,
    results_for_date,
    results_yesterday,
    settle_entries_for_date,
    settle_yesterday,
)


def handle_tracking_command(command, argv):
    if command == "settle_yesterday":
        if len(argv) > 2:
            return settle_entries_for_date(argv[2])
        return settle_yesterday()

    if command == "best_today":
        limit = int(argv[2]) if len(argv) > 2 else 15
        if len(argv) > 3:
            return best_plays_for_date(argv[3], limit=limit)
        return best_today(limit=limit)

    if command == "results_yesterday":
        limit = int(argv[2]) if len(argv) > 2 else 50
        if len(argv) > 3:
            return results_for_date(argv[3], limit=limit)
        return results_yesterday(limit=limit)

    if command == "export_training_rows":
        if len(argv) < 3:
            return {
                "error": (
                    "Usage: export_training_rows <output_path> "
                    "[format:csv|jsonl] [date_from:YYYY-MM-DD] [date_to:YYYY-MM-DD]"
                )
            }
        output_path = argv[2]
        fmt = argv[3] if len(argv) > 3 else None
        date_from = argv[4] if len(argv) > 4 else None
        date_to = argv[5] if len(argv) > 5 else None
        return export_training_rows(
            output_path=output_path,
            fmt=fmt,
            date_from=date_from,
            date_to=date_to,
        )

    if command == "record_closing":
        if len(argv) < 4:
            return {
                "error": (
                    "Usage: record_closing <date:YYYY-MM-DD> '<json_updates>' "
                    "where each update includes entryId + closingLine/closingOdds"
                )
            }
        date_str = argv[2]
        try:
            updates = json.loads(argv[3])
        except json.JSONDecodeError as je:
            return {"error": f"Invalid JSON for closing updates: {je}"}
        return record_closing_values(date_str, updates)

    return None
