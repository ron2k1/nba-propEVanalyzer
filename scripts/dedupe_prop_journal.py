#!/usr/bin/env python3
"""
One-time dedup of prop_journal.jsonl using _entry_key().

Groups by (pickDate, playerId, stat) — aligned with SQLite UNIQUE INDEX
(player_id, stat, game_date_ct).  Keeps the latest createdAtUtc per group.
Writes atomically.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.nba_bet_tracking import (
    JOURNAL_PATH,
    _load_journal_entries,
    _write_journal_entries,
    _entry_key,
)


def main():
    entries = _load_journal_entries()
    if not entries:
        print("No entries found.")
        return

    latest = {}
    for entry in entries:
        key = _entry_key(entry)
        ts = str(entry.get("createdAtUtc", ""))
        prev = latest.get(key)
        if prev is None or ts >= str(prev.get("createdAtUtc", "")):
            latest[key] = entry

    deduped = sorted(latest.values(), key=lambda e: str(e.get("createdAtUtc", "")))
    removed = len(entries) - len(deduped)

    print(f"{len(entries)} entries -> {len(deduped)} unique, {removed} duplicates removed")

    if removed > 0:
        _write_journal_entries(deduped)
        print(f"Written to {JOURNAL_PATH}")
    else:
        print("No duplicates found — journal unchanged.")


if __name__ == "__main__":
    main()
