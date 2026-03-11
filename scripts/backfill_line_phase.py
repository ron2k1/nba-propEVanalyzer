#!/usr/bin/env python3
"""
Backfill `line_phase` ("pregame" / "live" / "unknown") into existing
LineStore JSONL files under data/line_history/.

Uses `fetched_at` (per-request) when present, falls back to `timestamp_utc`.
Both are UTC ISO strings — compared directly against `commence_time` (also UTC).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "line_history")


def backfill_file(path):
    updated = 0
    total = 0
    lines_out = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw_stripped = raw.strip()
            if not raw_stripped:
                lines_out.append(raw)
                continue
            try:
                snap = json.loads(raw_stripped)
            except json.JSONDecodeError:
                lines_out.append(raw)
                continue
            total += 1
            if "line_phase" not in snap:
                ts = snap.get("fetched_at") or snap.get("timestamp_utc")
                ct = snap.get("commence_time")
                if ts and ct:
                    phase = "pregame" if ts < ct else "live"
                else:
                    phase = "unknown"
                snap["line_phase"] = phase
                updated += 1
            lines_out.append(json.dumps(snap, separators=(",", ":"), ensure_ascii=False) + "\n")

    if updated > 0:
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines_out)
    return total, updated


def main():
    if not os.path.isdir(DATA_DIR):
        print(f"No line_history directory found at {DATA_DIR}")
        return

    files = sorted(f for f in os.listdir(DATA_DIR) if f.endswith(".jsonl"))
    if not files:
        print("No JSONL files found.")
        return

    grand_total = 0
    grand_updated = 0
    for fname in files:
        fpath = os.path.join(DATA_DIR, fname)
        total, updated = backfill_file(fpath)
        grand_total += total
        grand_updated += updated
        if updated > 0:
            print(f"  {fname}: {updated}/{total} snapshots tagged")

    print(f"\nDone: {grand_updated}/{grand_total} snapshots across {len(files)} files backfilled with line_phase.")


if __name__ == "__main__":
    main()
