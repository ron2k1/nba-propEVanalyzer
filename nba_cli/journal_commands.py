#!/usr/bin/env python3
"""
CLI handlers for Decision Journal commands.

Commands:
  journal_settle  <date:YYYY-MM-DD> [--db <path>] [--odds-db <path>]
  journal_report  <date_from> <date_to> [--db <path>]
  journal_gate    [--window-days 14] [--min-sample 30] [--db <path>]
  journal_signals [date] [--stat s] [--limit N] [--db <path>]
  paper_summary   [--window-days 14] [--db <path>]
  paper_settle    <date:YYYY-MM-DD> [--db <path>] [--odds-db <path>]
"""

from core.nba_decision_journal import DecisionJournal


def handle_journal_command(command, argv):
    # -----------------------------------------------------------------------
    # journal_settle <date> [--db <path>] [--odds-db <path>]
    # -----------------------------------------------------------------------
    if command == "journal_settle":
        if len(argv) < 3:
            return {
                "error": "Usage: journal_settle <date:YYYY-MM-DD> [--db <path>] [--odds-db <path>]"
            }
        date_str = argv[2]
        db_path = None
        odds_db_path = None
        idx = 3
        while idx < len(argv):
            if argv[idx] == "--db" and idx + 1 < len(argv):
                db_path = argv[idx + 1]
                idx += 2
            elif argv[idx] == "--odds-db" and idx + 1 < len(argv):
                odds_db_path = argv[idx + 1]
                idx += 2
            else:
                idx += 1

        odds_store = None
        if odds_db_path:
            from core.nba_odds_store import OddsStore
            odds_store = OddsStore(db_path=odds_db_path)

        dj = DecisionJournal(db_path=db_path)
        try:
            result = dj.settle_signals_for_date(date_str, odds_store=odds_store)
        finally:
            dj.close()
            if odds_store is not None:
                odds_store.close()
        return result

    # -----------------------------------------------------------------------
    # journal_report <date_from> <date_to> [--db <path>]
    # -----------------------------------------------------------------------
    if command == "journal_report":
        if len(argv) < 4:
            return {
                "error": (
                    "Usage: journal_report <date_from:YYYY-MM-DD> <date_to:YYYY-MM-DD> "
                    "[--db <path>]"
                )
            }
        date_from = argv[2]
        date_to = argv[3]
        db_path = None
        idx = 4
        while idx < len(argv):
            if argv[idx] == "--db" and idx + 1 < len(argv):
                db_path = argv[idx + 1]
                idx += 2
            else:
                idx += 1

        dj = DecisionJournal(db_path=db_path)
        try:
            result = dj.generate_report(date_from, date_to)
        finally:
            dj.close()
        return result

    # -----------------------------------------------------------------------
    # journal_gate [--window-days N] [--min-sample N] [--db <path>]
    # -----------------------------------------------------------------------
    if command == "journal_gate":
        window_days = 14
        min_sample = 30
        db_path = None
        idx = 2
        while idx < len(argv):
            if argv[idx] == "--window-days" and idx + 1 < len(argv):
                try:
                    window_days = int(argv[idx + 1])
                except ValueError:
                    pass
                idx += 2
            elif argv[idx] == "--min-sample" and idx + 1 < len(argv):
                try:
                    min_sample = int(argv[idx + 1])
                except ValueError:
                    pass
                idx += 2
            elif argv[idx] == "--db" and idx + 1 < len(argv):
                db_path = argv[idx + 1]
                idx += 2
            else:
                idx += 1

        dj = DecisionJournal(db_path=db_path)
        try:
            result = dj.gate_check(window_days=window_days, min_sample=min_sample)
        finally:
            dj.close()
        return result

    # -----------------------------------------------------------------------
    # journal_signals [date] [--stat s] [--limit N] [--db <path>]
    # -----------------------------------------------------------------------
    if command == "journal_signals":
        date_str = None
        stat = None
        limit = 50
        db_path = None
        idx = 2
        while idx < len(argv):
            tok = argv[idx]
            if tok == "--stat" and idx + 1 < len(argv):
                stat = argv[idx + 1]
                idx += 2
            elif tok == "--limit" and idx + 1 < len(argv):
                try:
                    limit = int(argv[idx + 1])
                except ValueError:
                    pass
                idx += 2
            elif tok == "--db" and idx + 1 < len(argv):
                db_path = argv[idx + 1]
                idx += 2
            elif not tok.startswith("--") and date_str is None:
                date_str = tok
                idx += 1
            else:
                idx += 1

        dj = DecisionJournal(db_path=db_path)
        try:
            result = dj.get_signals(date_str=date_str, stat=stat, limit=limit)
        finally:
            dj.close()
        return result

    # -----------------------------------------------------------------------
    # paper_summary [--window-days N] [--db <path>]
    # Combined report + gate check for forward validation monitoring.
    # -----------------------------------------------------------------------
    if command == "paper_summary":
        from datetime import datetime, timedelta
        window_days = 14
        db_path = None
        idx = 2
        while idx < len(argv):
            if argv[idx] == "--window-days" and idx + 1 < len(argv):
                try:
                    window_days = int(argv[idx + 1])
                except ValueError:
                    pass
                idx += 2
            elif argv[idx] == "--db" and idx + 1 < len(argv):
                db_path = argv[idx + 1]
                idx += 2
            else:
                idx += 1

        date_to = datetime.now().date()
        date_from = date_to - timedelta(days=window_days)
        dj = DecisionJournal(db_path=db_path)
        try:
            report = dj.generate_report(date_from.isoformat(), date_to.isoformat())
            gate = dj.gate_check(window_days=window_days)
        finally:
            dj.close()
        return {
            "success": True,
            "report": report,
            "gate": gate,
        }

    # -----------------------------------------------------------------------
    # paper_settle <date:YYYY-MM-DD> [--db <path>] [--odds-db <path>]
    # Settles BOTH prop_journal.jsonl AND decision_journal.sqlite for a date.
    # -----------------------------------------------------------------------
    if command == "paper_settle":
        if len(argv) < 3:
            return {
                "error": "Usage: paper_settle <date:YYYY-MM-DD> [--db <path>] [--odds-db <path>]"
            }
        date_str = argv[2]
        db_path = None
        odds_db_path = None
        idx = 3
        while idx < len(argv):
            if argv[idx] == "--db" and idx + 1 < len(argv):
                db_path = argv[idx + 1]
                idx += 2
            elif argv[idx] == "--odds-db" and idx + 1 < len(argv):
                odds_db_path = argv[idx + 1]
                idx += 2
            else:
                idx += 1

        from core.nba_bet_tracking import settle_entries_for_date

        odds_store = None
        if odds_db_path:
            from core.nba_odds_store import OddsStore
            odds_store = OddsStore(db_path=odds_db_path)

        jsonl_result = settle_entries_for_date(date_str)
        dj = DecisionJournal(db_path=db_path)
        try:
            sqlite_result = dj.settle_signals_for_date(date_str, odds_store=odds_store)
        finally:
            dj.close()
            if odds_store is not None:
                odds_store.close()

        return {
            "success": True,
            "date": date_str,
            "jsonlJournal": jsonl_result,
            "decisionJournal": sqlite_result,
        }

    return None
