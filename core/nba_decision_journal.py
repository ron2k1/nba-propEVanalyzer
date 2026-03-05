#!/usr/bin/env python3
"""
Decision Journal — SQLite-backed signal logger and performance tracker.

Answers "is the edge real?" using pre-outcome signals and post-outcome settlement.

Tables
------
signals  - pre-outcome EV signals that pass the quality filter
outcomes - post-settlement win/loss/push + CLV data
"""

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from os.path import abspath, dirname, join

_log = logging.getLogger("nba_engine.journal")

_ROOT = dirname(dirname(abspath(__file__)))
_DEFAULT_DB_PATH = join(_ROOT, "data", "decision_journal", "decision_journal.sqlite")


def _ct_day_utc_bounds(date_str):
    """
    Return (utc_start_iso, utc_end_exclusive_iso) for a US Central Time calendar day.

    Accounts for CDT (UTC-5) vs CST (UTC-6):
      CDT active: 2nd Sunday in March through (not including) 1st Sunday in November
      CST active: all other dates

    Example:
      _ct_day_utc_bounds("2026-04-15")  -> ("2026-04-15T05:00:00Z", "2026-04-16T05:00:00Z")
      _ct_day_utc_bounds("2026-01-10")  -> ("2026-01-10T06:00:00Z", "2026-01-11T06:00:00Z")
    """
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    y = d.year
    # 2nd Sunday in March (clocks spring forward at 2am CST → 3am CDT)
    mar1 = datetime(y, 3, 1)
    first_sun_mar = mar1 + timedelta(days=(6 - mar1.weekday()) % 7)
    dst_start = (first_sun_mar + timedelta(weeks=1)).date()
    # 1st Sunday in November (clocks fall back at 2am CDT → 1am CST)
    nov1 = datetime(y, 11, 1)
    dst_end = (nov1 + timedelta(days=(6 - nov1.weekday()) % 7)).date()

    def _utc_offset_h(day):
        return 5 if (dst_start <= day < dst_end) else 6

    next_d = d + timedelta(days=1)
    start_utc = datetime(d.year, d.month, d.day, _utc_offset_h(d), 0, 0)
    end_utc   = datetime(next_d.year, next_d.month, next_d.day, _utc_offset_h(next_d), 0, 0)
    return (
        start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


from .nba_data_collection import safe_round
from .nba_bet_tracking import (
    _now_utc_iso,
    _as_float,
    _as_int,
    _season_from_date,
    _fetch_player_logs,
    _find_game_row,
    _extract_stat_from_row,
    _grade_side,
    _pnl_for_outcome,
    _clv_line_delta,
)

# ---------------------------------------------------------------------------
# Gate logic — canonical source is core/gates.py; re-exported here for
# backward compatibility so existing importers don't break.
# ---------------------------------------------------------------------------
from .gates import SIGNAL_SPEC, CURRENT_SIGNAL_VERSION, _qualifies  # re-export
from .nba_data_collection import BETTING_POLICY

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id        TEXT PRIMARY KEY,
    ts_utc           TEXT NOT NULL,
    signal_version   TEXT NOT NULL DEFAULT 'v1',
    player_id        INTEGER,
    player_name      TEXT,
    team_abbr        TEXT,
    opponent_abbr    TEXT,
    stat             TEXT,
    line             REAL,
    book             TEXT,
    over_odds        INTEGER,
    under_odds       INTEGER,
    projection       REAL,
    prob_over        REAL,
    prob_under       REAL,
    edge_over        REAL,
    edge_under       REAL,
    recommended_side TEXT,
    recommended_edge REAL,
    confidence       REAL,
    used_real_line   INTEGER DEFAULT 0,
    action_taken     INTEGER DEFAULT 0,
    skip_reason      TEXT,
    context_json     TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_dedup
    ON signals (player_id, stat, book, line, substr(ts_utc, 1, 10));

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id        TEXT NOT NULL REFERENCES signals(signal_id),
    game_id          TEXT,
    settle_date      TEXT,
    result           TEXT CHECK (result IN ('win','loss','push')),
    pnl_units        REAL,
    close_line       REAL,
    close_over_odds  INTEGER,
    close_under_odds INTEGER,
    clv_delta        REAL,
    settled_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_outcomes_signal_id   ON outcomes(signal_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_settle_date ON outcomes(settle_date);
CREATE INDEX IF NOT EXISTS idx_signals_stat         ON signals(stat);
CREATE INDEX IF NOT EXISTS idx_signals_ts_utc       ON signals(ts_utc);
"""


# ---------------------------------------------------------------------------
# DecisionJournal class
# ---------------------------------------------------------------------------

class DecisionJournal:
    """SQLite-backed decision journal for signal logging and settlement."""

    def __init__(self, db_path=None):
        self._path = db_path or _DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _init_db(self):
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Signal logging
    # ------------------------------------------------------------------

    def log_signal(
        self, *,
        player_id, player_name, team_abbr, opponent_abbr,
        stat, line, book, over_odds, under_odds,
        projection, prob_over, prob_under,
        edge_over, edge_under, recommended_side, recommended_edge, confidence,
        used_real_line=False, action_taken=0, skip_reason=None,
        context=None, signal_version=CURRENT_SIGNAL_VERSION,
    ) -> dict:
        """Log a qualifying signal. Returns {success, signalId|None, isDuplicate}."""
        import json as _json
        signal_id = str(uuid.uuid4())
        ts_utc = _now_utc_iso()
        context_json = _json.dumps(context, separators=(",", ":")) if context else None
        try:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO signals (
                    signal_id, ts_utc, signal_version,
                    player_id, player_name, team_abbr, opponent_abbr,
                    stat, line, book, over_odds, under_odds,
                    projection, prob_over, prob_under,
                    edge_over, edge_under, recommended_side, recommended_edge, confidence,
                    used_real_line, action_taken, skip_reason, context_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    signal_id, ts_utc, signal_version,
                    int(player_id) if player_id is not None else None,
                    str(player_name or ""),
                    str(team_abbr or "").upper(),
                    str(opponent_abbr or "").upper(),
                    str(stat or "").lower(),
                    float(line) if line is not None else None,
                    str(book or ""),
                    int(over_odds) if over_odds is not None else None,
                    int(under_odds) if under_odds is not None else None,
                    float(projection) if projection is not None else None,
                    float(prob_over) if prob_over is not None else None,
                    float(prob_under) if prob_under is not None else None,
                    float(edge_over) if edge_over is not None else None,
                    float(edge_under) if edge_under is not None else None,
                    str(recommended_side or ""),
                    float(recommended_edge) if recommended_edge is not None else None,
                    float(confidence) if confidence is not None else None,
                    1 if used_real_line else 0,
                    int(action_taken) if action_taken is not None else 0,
                    str(skip_reason) if skip_reason else None,
                    context_json,
                ),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return {"success": True, "signalId": None, "isDuplicate": True}
            return {"success": True, "signalId": signal_id, "isDuplicate": False}
        except Exception as e:
            return {"success": False, "error": str(e), "signalId": None, "isDuplicate": False}

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def settle_signals_for_date(self, date_str, odds_store=None) -> dict:
        """Settle unsettled signals for date_str. Optionally enrich CLV from odds_store."""
        import time
        from .nba_odds_store import STAT_TO_MARKET
        try:
            date_obj = datetime.strptime(str(date_str), "%Y-%m-%d").date()
        except ValueError:
            return {"success": False, "error": "Invalid date. Use YYYY-MM-DD.", "date": str(date_str)}

        utc_start, utc_end = _ct_day_utc_bounds(date_str)
        cur = self._conn.execute(
            """SELECT s.signal_id, s.player_id, s.player_name, s.team_abbr, s.opponent_abbr,
                      s.stat, s.line, s.book, s.over_odds, s.under_odds,
                      s.recommended_side, s.recommended_edge
               FROM signals s
               WHERE s.ts_utc >= ? AND s.ts_utc < ?
               AND s.signal_id NOT IN (SELECT signal_id FROM outcomes)""",
            (utc_start, utc_end),
        )
        rows = cur.fetchall()
        cols = [
            "signal_id", "player_id", "player_name", "team_abbr", "opponent_abbr",
            "stat", "line", "book", "over_odds", "under_odds", "recommended_side", "recommended_edge",
        ]
        pending = [dict(zip(cols, r)) for r in rows]
        if not pending:
            return {"success": True, "date": date_str, "settled": 0, "unresolved": 0, "errors": 0}

        season = _season_from_date(date_obj)
        logs_cache = {}
        settled = 0
        unresolved = 0
        errors = 0

        for sig in pending:
            player_id = _as_int(sig.get("player_id"), 0)
            if player_id <= 0:
                unresolved += 1
                continue

            cache_key = (player_id, season)
            if cache_key not in logs_cache:
                if logs_cache:
                    time.sleep(0.6)
                try:
                    logs_cache[cache_key] = _fetch_player_logs(player_id, season)
                except Exception:
                    logs_cache[cache_key] = []

            row = _find_game_row(logs_cache[cache_key], date_obj)
            if not row:
                unresolved += 1
                continue

            actual = _extract_stat_from_row(row, sig.get("stat"))
            if actual is None:
                unresolved += 1
                continue

            line = _as_float(sig.get("line"))
            rec_side = str(sig.get("recommended_side") or "").lower()
            result = _grade_side(actual, line, rec_side)
            if result not in ("win", "loss", "push"):
                unresolved += 1
                continue

            rec_odds = sig.get("over_odds") if rec_side == "over" else sig.get("under_odds")
            pnl = _pnl_for_outcome(result, rec_odds)

            # CLV enrichment (optional)
            clv_delta = None
            close_line = None
            close_over_odds = None
            close_under_odds = None
            if odds_store is not None:
                try:
                    stat_key = str(sig.get("stat") or "").lower()
                    market = STAT_TO_MARKET.get(stat_key)
                    if market:
                        player_name_sig = sig.get("player_name", "")
                        event_id = odds_store.find_event_for_game(
                            sig.get("team_abbr", ""), sig.get("opponent_abbr", ""), date_str
                        )
                        if event_id:
                            cl = odds_store.get_closing_line(
                                event_id, market, player_name_sig
                            )
                        else:
                            # Snapshots missing for this game — look up by player+date directly
                            cl = odds_store.get_closing_line_by_player_date(
                                player_name_sig, market, date_str
                            )
                        if cl:
                            close_line = cl.get("close_line")
                            close_over_odds = cl.get("close_over_odds")
                            close_under_odds = cl.get("close_under_odds")
                            clv_delta = _clv_line_delta(rec_side, line, close_line)
                except Exception as clv_ex:
                    _log.warning("CLV enrichment failed for signal %s: %s",
                                 sig.get("signal_id", "?"), clv_ex)

            try:
                self._conn.execute(
                    """INSERT INTO outcomes
                       (signal_id, settle_date, result, pnl_units,
                        close_line, close_over_odds, close_under_odds, clv_delta, settled_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        sig["signal_id"], date_str, result, pnl,
                        close_line, close_over_odds, close_under_odds, clv_delta,
                        _now_utc_iso(),
                    ),
                )
                self._conn.commit()
                settled += 1
            except Exception:
                errors += 1

        return {
            "success": True,
            "date": date_str,
            "settled": settled,
            "unresolved": unresolved,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def generate_report(self, date_from, date_to) -> dict:
        """Generate a performance report over a date range.

        Includes collection coverage: dates with signals vs total calendar
        dates, plus any gap dates where no signals were recorded.
        """
        utc_start, _    = _ct_day_utc_bounds(date_from)
        _,        utc_end = _ct_day_utc_bounds(date_to)
        cur = self._conn.execute(
            """SELECT s.signal_id, s.stat, s.confidence, s.recommended_edge, s.used_real_line,
                      s.action_taken,
                      o.result, o.pnl_units, o.clv_delta
               FROM signals s
               LEFT JOIN outcomes o ON s.signal_id = o.signal_id
               WHERE s.ts_utc >= ? AND s.ts_utc < ?""",
            (utc_start, utc_end),
        )
        rows = cur.fetchall()
        cols = [
            "signal_id", "stat", "confidence", "recommended_edge", "used_real_line",
            "action_taken", "result", "pnl_units", "clv_delta",
        ]
        records = [dict(zip(cols, r)) for r in rows]

        qualifying_count = len(records)
        taken_count = sum(1 for r in records if r.get("action_taken"))
        skipped_count = qualifying_count - taken_count
        settled_records = [r for r in records if r.get("result") in ("win", "loss", "push")]
        settled_count = len(settled_records)

        def _stats(recs):
            graded = [r for r in recs if r.get("result") in ("win", "loss", "push")]
            if not graded:
                return None, None
            wins = sum(1 for r in graded if r.get("result") == "win")
            non_push = sum(1 for r in graded if r.get("result") in ("win", "loss"))
            hit_rate = safe_round(wins / non_push, 4) if non_push > 0 else None
            total_pnl = sum(_as_float(r.get("pnl_units"), 0.0) for r in graded)
            roi = safe_round(total_pnl / len(graded), 4) if graded else None
            return hit_rate, roi

        hit_rate_taken, roi_taken = _stats([r for r in records if r.get("action_taken")])
        hit_rate_all, roi_all = _stats(records)

        clv_taken = [
            r for r in settled_records
            if r.get("action_taken") and r.get("clv_delta") is not None
        ]
        clv_all = [r for r in settled_records if r.get("clv_delta") is not None]
        avg_clv_taken = (
            safe_round(sum(r["clv_delta"] for r in clv_taken) / len(clv_taken), 4)
            if clv_taken else None
        )
        avg_clv_all = (
            safe_round(sum(r["clv_delta"] for r in clv_all) / len(clv_all), 4)
            if clv_all else None
        )

        # By stat
        by_stat: dict = {}
        for r in records:
            s = str(r.get("stat") or "unknown")
            by_stat.setdefault(s, []).append(r)
        by_stat_out = {}
        for s, recs in by_stat.items():
            hr, roi = _stats(recs)
            by_stat_out[s] = {
                "count": len(recs),
                "settled": sum(1 for r in recs if r.get("result") in ("win", "loss", "push")),
                "hitRate": hr,
                "roi": roi,
            }

        # By confidence bucket
        conf_buckets = [
            ("0.55-0.60", 0.55, 0.60), ("0.60-0.65", 0.60, 0.65),
            ("0.65-0.70", 0.65, 0.70), ("0.70+", 0.70, 1.01),
        ]
        by_conf = {}
        for label, lo, hi in conf_buckets:
            recs = [r for r in records if lo <= _as_float(r.get("confidence"), 0.0) < hi]
            hr, roi = _stats(recs)
            by_conf[label] = {"count": len(recs), "hitRate": hr, "roi": roi}

        # By edge bucket
        edge_buckets = [
            ("0.05-0.07", 0.05, 0.07), ("0.07-0.10", 0.07, 0.10), ("0.10+", 0.10, 10.0),
        ]
        by_edge = {}
        for label, lo, hi in edge_buckets:
            recs = [r for r in records if lo <= _as_float(r.get("recommended_edge"), 0.0) < hi]
            hr, roi = _stats(recs)
            by_edge[label] = {"count": len(recs), "hitRate": hr, "roi": roi}

        # By line type
        real_recs = [r for r in records if r.get("used_real_line")]
        synth_recs = [r for r in records if not r.get("used_real_line")]
        hr_real, roi_real = _stats(real_recs)
        hr_synth, roi_synth = _stats(synth_recs)
        by_line_type = {
            "real": {"count": len(real_recs), "hitRate": hr_real, "roi": roi_real},
            "synthetic": {"count": len(synth_recs), "hitRate": hr_synth, "roi": roi_synth},
        }

        # --- Collection coverage / gap detection ---
        date_from_d = datetime.strptime(str(date_from), "%Y-%m-%d").date()
        date_to_d = datetime.strptime(str(date_to), "%Y-%m-%d").date()
        all_dates = set()
        d = date_from_d
        while d < date_to_d:
            all_dates.add(d.isoformat())
            d += timedelta(days=1)

        # Signal ts_utc is stored in UTC; shift by -6h to recover the
        # Central Time game day (matches _ct_day_utc_bounds convention).
        ct_signal_dates_cur = self._conn.execute(
            """SELECT DISTINCT DATE(ts_utc, '-6 hours') AS ct_date
               FROM signals
               WHERE ts_utc >= ? AND ts_utc < ?""",
            (utc_start, utc_end),
        )
        dates_with_signals = {row[0] for row in ct_signal_dates_cur.fetchall()}
        gaps = sorted(all_dates - dates_with_signals)

        coverage = {
            "daysInRange": len(all_dates),
            "daysWithSignals": len(dates_with_signals & all_dates),
            "gapDates": gaps,
        }
        if gaps:
            coverage["warning"] = f"{len(gaps)} collection day(s) missing"

        return {
            "success": True,
            "dateFrom": date_from,
            "dateTo": date_to,
            "qualifying_count": qualifying_count,
            "taken_count": taken_count,
            "skipped_count": skipped_count,
            "settled_count": settled_count,
            "hit_rate_taken": hit_rate_taken,
            "hit_rate_all": hit_rate_all,
            "roi_taken": roi_taken,
            "roi_all": roi_all,
            "avg_clv_taken": avg_clv_taken,
            "avg_clv_all": avg_clv_all,
            "by_stat": by_stat_out,
            "by_confidence_bucket": by_conf,
            "by_edge_bucket": by_edge,
            "by_line_type": by_line_type,
            "coverage": coverage,
        }

    # ------------------------------------------------------------------
    # Gate check
    # ------------------------------------------------------------------

    def gate_check(
        self,
        window_days=14,
        min_sample=50,
        min_roi=0.0,
        min_positive_clv_pct=50.0,
    ) -> dict:
        """Rolling gate check over last window_days of settled outcomes."""
        date_to = datetime.now(timezone.utc).date()
        date_from = date_to - timedelta(days=window_days)
        date_from_str = date_from.isoformat()
        date_to_str = date_to.isoformat()

        cur = self._conn.execute(
            """SELECT s.stat, s.action_taken, s.recommended_edge,
                      o.result, o.pnl_units, o.clv_delta
               FROM signals s
               JOIN outcomes o ON s.signal_id = o.signal_id
               WHERE o.settle_date >= ? AND o.settle_date <= ?
               AND o.result IN ('win','loss','push')""",
            (date_from_str, date_to_str),
        )
        rows = cur.fetchall()
        cols = ["stat", "action_taken", "recommended_edge", "result", "pnl_units", "clv_delta"]
        all_records = [dict(zip(cols, r)) for r in rows]
        # Filter to BETTING_POLICY stat_whitelist — reb signals are logged for
        # research but must NOT count toward the GO-LIVE gate.
        _wl = BETTING_POLICY.get("stat_whitelist")
        records = [r for r in all_records if not _wl or r.get("stat") in _wl]

        sample = len(records)
        wins = sum(1 for r in records if r.get("result") == "win")
        non_push = sum(1 for r in records if r.get("result") in ("win", "loss"))
        hit_rate = safe_round(wins / non_push, 4) if non_push > 0 else None
        total_pnl = sum(_as_float(r.get("pnl_units"), 0.0) for r in records)
        roi = safe_round(total_pnl / sample, 4) if sample > 0 else None

        clv_recs = [r for r in records if r.get("clv_delta") is not None]
        positive_clv_count = sum(
            1 for r in clv_recs if (_as_float(r.get("clv_delta"), 0.0) or 0.0) > 0
        )
        positive_clv_pct = (
            safe_round(positive_clv_count / len(clv_recs) * 100.0, 2) if clv_recs else None
        )

        reasons = []
        gate_pass = True
        if sample < min_sample:
            gate_pass = False
            reasons.append(f"insufficient_sample:{sample}<{min_sample}")
        if roi is not None and roi < min_roi:
            gate_pass = False
            reasons.append(f"roi_below_threshold:{roi:.4f}<{min_roi}")
        if positive_clv_pct is not None and positive_clv_pct < min_positive_clv_pct:
            gate_pass = False
            reasons.append(f"positive_clv_pct_below_threshold:{positive_clv_pct:.1f}<{min_positive_clv_pct}")

        # Disabled stats (informational): ≥20 signals AND hit_rate < 45%
        stat_groups: dict = {}
        for r in records:
            s = str(r.get("stat") or "")
            stat_groups.setdefault(s, []).append(r)
        disabled_stats = []
        for s, recs in stat_groups.items():
            if len(recs) >= 20:
                w = sum(1 for r in recs if r.get("result") == "win")
                np_count = sum(1 for r in recs if r.get("result") in ("win", "loss"))
                if np_count > 0 and w / np_count < 0.45:
                    disabled_stats.append(s)

        # Research-only stats (signal-eligible but NOT in betting whitelist)
        research_records = [r for r in all_records if _wl and r.get("stat") not in _wl]
        research_by_stat: dict = {}
        for r in research_records:
            s = str(r.get("stat") or "")
            research_by_stat.setdefault(s, []).append(r)
        research_out = {}
        for s, recs in research_by_stat.items():
            w = sum(1 for r in recs if r.get("result") == "win")
            np_c = sum(1 for r in recs if r.get("result") in ("win", "loss"))
            research_out[s] = {
                "count": len(recs),
                "wins": w,
                "hitRate": safe_round(w / np_c, 4) if np_c > 0 else None,
                "pnl": safe_round(sum(_as_float(r.get("pnl_units"), 0.0) for r in recs), 2),
            }

        # Model leans: ALL qualifying signals regardless of BETTING_POLICY
        # (policy-qualified + research-only). Shows raw model predictive ability.
        leans_sample = len(all_records)
        leans_wins = sum(1 for r in all_records if r.get("result") == "win")
        leans_np = sum(1 for r in all_records if r.get("result") in ("win", "loss"))
        leans_pnl = sum(_as_float(r.get("pnl_units"), 0.0) for r in all_records)

        # Edge-at-emission analysis — sustainability signal
        policy_edges = [_as_float(r.get("recommended_edge"), 0.0) for r in records]
        all_edges = [_as_float(r.get("recommended_edge"), 0.0) for r in all_records]

        return {
            "gatePass": gate_pass,
            "reason": "; ".join(reasons) if reasons else "all_checks_passed",
            "windowDays": window_days,
            "windowFrom": date_from_str,
            "windowTo": date_to_str,
            "metrics": {
                "sample": sample,
                "hit_rate": hit_rate,
                "roi": roi,
                "positive_clv_pct": positive_clv_pct,
            },
            "disabled_stats": disabled_stats,
            "research_stats": research_out,
            "model_leans": {
                "sample": leans_sample,
                "wins": leans_wins,
                "hitRate": safe_round(leans_wins / leans_np, 4) if leans_np > 0 else None,
                "roi": safe_round(leans_pnl / leans_sample, 4) if leans_sample > 0 else None,
                "pnl": safe_round(leans_pnl, 2),
                "avgEdge": safe_round(sum(all_edges) / len(all_edges), 4) if all_edges else None,
                "note": "All SIGNAL_SPEC-eligible signals including research-only stats",
            },
            "edge_at_emission": {
                "policy": {
                    "avgEdge": safe_round(sum(policy_edges) / len(policy_edges), 4) if policy_edges else None,
                    "minEdge": safe_round(min(policy_edges), 4) if policy_edges else None,
                    "maxEdge": safe_round(max(policy_edges), 4) if policy_edges else None,
                },
                "all": {
                    "avgEdge": safe_round(sum(all_edges) / len(all_edges), 4) if all_edges else None,
                    "minEdge": safe_round(min(all_edges), 4) if all_edges else None,
                    "maxEdge": safe_round(max(all_edges), 4) if all_edges else None,
                },
            },
            "config": {
                "min_sample": min_sample,
                "min_roi": min_roi,
                "min_positive_clv_pct": min_positive_clv_pct,
                "stat_whitelist": sorted(BETTING_POLICY.get("stat_whitelist", set())),
            },
        }

    # ------------------------------------------------------------------
    # CLV backfill — retroactively populate clv_delta for old outcomes
    # ------------------------------------------------------------------

    def backfill_clv(self, odds_store) -> dict:
        """Retroactively compute CLV for all settled outcomes with clv_delta IS NULL."""
        from .nba_odds_store import STAT_TO_MARKET
        cur = self._conn.execute(
            """SELECT o.outcome_id, o.settle_date,
                      s.player_name, s.team_abbr, s.opponent_abbr,
                      s.stat, s.line, s.recommended_side
               FROM outcomes o
               JOIN signals s ON s.signal_id = o.signal_id
               WHERE o.clv_delta IS NULL AND o.result IS NOT NULL""",
        )
        rows = cur.fetchall()
        cols = [
            "outcome_id", "settle_date", "player_name", "team_abbr", "opponent_abbr",
            "stat", "line", "recommended_side",
        ]
        records = [dict(zip(cols, r)) for r in rows]

        filled = 0
        skipped = 0
        for rec in records:
            try:
                stat_key = str(rec.get("stat") or "").lower()
                market = STAT_TO_MARKET.get(stat_key)
                if not market:
                    skipped += 1
                    continue
                date_str = str(rec.get("settle_date") or "")
                player_name_rec = rec.get("player_name", "")
                event_id = odds_store.find_event_for_game(
                    rec.get("team_abbr", ""), rec.get("opponent_abbr", ""), date_str
                )
                if event_id:
                    cl = odds_store.get_closing_line(event_id, market, player_name_rec)
                else:
                    cl = odds_store.get_closing_line_by_player_date(
                        player_name_rec, market, date_str
                    )
                if not cl:
                    skipped += 1
                    continue
                close_line = cl.get("close_line")
                clv_delta = _clv_line_delta(
                    str(rec.get("recommended_side") or ""),
                    _as_float(rec.get("line")),
                    close_line,
                )
                self._conn.execute(
                    """UPDATE outcomes
                       SET clv_delta=?, close_line=?, close_over_odds=?, close_under_odds=?
                       WHERE outcome_id=?""",
                    (
                        clv_delta, close_line,
                        cl.get("close_over_odds"), cl.get("close_under_odds"),
                        rec["outcome_id"],
                    ),
                )
                filled += 1
            except Exception:
                skipped += 1

        self._conn.commit()
        return {
            "success": True,
            "totalOutcomes": len(records),
            "filled": filled,
            "skipped": skipped,
        }

    # ------------------------------------------------------------------
    # Signal listing
    # ------------------------------------------------------------------

    def get_signals(self, date_str=None, stat=None, limit=50) -> dict:
        """Read-only signal listing."""
        clauses, vals = [], []
        if date_str:
            _gs_start, _gs_end = _ct_day_utc_bounds(date_str)
            clauses.append("s.ts_utc >= ? AND s.ts_utc < ?")
            vals.extend([_gs_start, _gs_end])
        if stat:
            clauses.append("s.stat=?")
            vals.append(str(stat).lower())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit_val = max(1, _as_int(limit, 50))
        cur = self._conn.execute(
            f"""SELECT s.signal_id, s.ts_utc, s.player_name, s.stat, s.line,
                       s.book, s.recommended_side, s.recommended_edge, s.confidence,
                       s.used_real_line, s.action_taken,
                       o.result, o.pnl_units, o.clv_delta
                FROM signals s
                LEFT JOIN outcomes o ON s.signal_id = o.signal_id
                {where}
                ORDER BY s.ts_utc DESC
                LIMIT ?""",
            vals + [limit_val],
        )
        signal_cols = [
            "signalId", "tsUtc", "playerName", "stat", "line",
            "book", "recommendedSide", "recommendedEdge", "confidence",
            "usedRealLine", "actionTaken", "result", "pnlUnits", "clvDelta",
        ]
        signals = [dict(zip(signal_cols, r)) for r in cur.fetchall()]
        return {
            "success": True,
            "date": date_str,
            "count": len(signals),
            "signals": signals,
        }
