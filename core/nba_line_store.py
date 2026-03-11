#!/usr/bin/env python3
"""
Line-history store: saves timestamped prop-line snapshots and computes CLV.

Storage layout:
  data/line_history/YYYY-MM-DD.jsonl  – one snapshot per line, appended on each poll
  data/alerts/YYYY-MM-DD.jsonl        – injury / stale-line / minutes alerts
  data/injury_snapshots/status.json   – rolling player-status dict for diff detection

Snapshot schema (enforced on write):
  timestamp_utc    str   ISO 8601 UTC (e.g. "2026-02-27T14:05:00Z")
  game_id          str   Odds API event ID
  player_name      str
  player_team_abbr str   may be "" when not known
  opponent_abbr    str   may be "" when not known
  is_home          bool  may be None when not known
  stat             str   pts / reb / ast / fg3m / stl / blk / tov / pra
  line             float
  over_odds        int   American
  under_odds       int   American
  book             str   betmgm / draftkings / fanduel / pinnacle / etc.

Recommendation / alert schema:
  alert_id         str   uuid
  generated_at     str   ISO 8601 UTC
  reason_type      str   injury | stale_line | minutes | poisson
  edge_pct         float model edge against stale book line
  confidence       str   low / medium / high
  book             str
  line             float current stale line
  model_projection float
  player_name      str
  stat             str
  recommended_side str   over | under
"""

import json
import os
import re
import statistics
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT            = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINE_HISTORY_DIR = os.path.join(_ROOT, "data", "line_history")
ALERTS_DIR       = os.path.join(_ROOT, "data", "alerts")
INJURY_SNAP_DIR  = os.path.join(_ROOT, "data", "injury_snapshots")
_JOURNAL_PATH    = os.path.join(_ROOT, "data", "prop_journal.jsonl")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _american_to_decimal(american_odds) -> float:
    try:
        p = float(american_odds)
    except (TypeError, ValueError):
        return 0.0
    if p == 0:
        return 0.0
    return 1.0 + (p / 100.0) if p > 0 else 1.0 + (100.0 / abs(p))


def _normalize_name(value: str) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", raw)).strip()


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _names_match(a: str, b: str) -> bool:
    ta = [t for t in _normalize_name(a).split() if t not in _SUFFIXES]
    tb = [t for t in _normalize_name(b).split() if t not in _SUFFIXES]
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    if len(ta) >= 2 and len(tb) >= 2:
        return ta[0] == tb[0] and ta[-1] == tb[-1]
    return False


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# LineStore
# ---------------------------------------------------------------------------

class LineStore:
    """
    Append-only JSONL store for prop-line snapshots.
    Thread-safe for read-many / sequential-write workloads.
    """

    def __init__(self, data_dir: str = None):
        self._dir = data_dir or LINE_HISTORY_DIR
        os.makedirs(self._dir, exist_ok=True)
        os.makedirs(ALERTS_DIR, exist_ok=True)
        os.makedirs(INJURY_SNAP_DIR, exist_ok=True)

    # -----------------------------------------------------------------------
    # Write
    # -----------------------------------------------------------------------

    def _path_for_date(self, date_str: str) -> str:
        return os.path.join(self._dir, f"{str(date_str)[:10]}.jsonl")

    def append_snapshot(self, snapshot: dict) -> None:
        ts = snapshot.get("timestamp_utc") or _utc_now_iso()
        date_str = str(ts)[:10]
        with open(self._path_for_date(date_str), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(snapshot, default=str) + "\n")

    def append_snapshots(self, snapshots: list) -> int:
        for s in snapshots:
            self.append_snapshot(s)
        return len(snapshots)

    # -----------------------------------------------------------------------
    # Read
    # -----------------------------------------------------------------------

    def get_snapshots(
        self,
        date_str: str,
        book: str = None,
        stat: str = None,
        player_name: str = None,
        phase: str = None,
    ) -> list:
        path = self._path_for_date(date_str)
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if book and row.get("book") != book:
                    continue
                if stat and row.get("stat") != stat:
                    continue
                if player_name and not _names_match(player_name, row.get("player_name", "")):
                    continue
                if phase and row.get("line_phase", "pregame") != phase:
                    continue
                rows.append(row)
        return rows

    def get_opening_line(self, date_str: str, player_name: str, stat: str, book: str = None, phase: str = None) -> dict:
        snaps = self.get_snapshots(date_str, book=book, stat=stat, player_name=player_name, phase=phase)
        if not snaps:
            return None
        return min(snaps, key=lambda x: x.get("timestamp_utc", ""))

    def get_closing_line(self, date_str: str, player_name: str, stat: str, book: str = None, phase: str = None) -> dict:
        snaps = self.get_snapshots(date_str, book=book, stat=stat, player_name=player_name, phase=phase)
        if not snaps:
            return None
        return max(snaps, key=lambda x: x.get("timestamp_utc", ""))

    def get_line_movement(self, date_str: str, player_name: str, stat: str, book: str = None, phase: str = None) -> list:
        snaps = self.get_snapshots(date_str, book=book, stat=stat, player_name=player_name, phase=phase)
        return sorted(snaps, key=lambda x: x.get("timestamp_utc", ""))

    def snapshot_count(self, date_str: str) -> int:
        return len(self.get_snapshots(date_str))

    # -----------------------------------------------------------------------
    # CLV computation
    # -----------------------------------------------------------------------

    def compute_clv(self, journal_entry: dict, date_str: str) -> dict:
        """
        Compute closing-line value for one journal entry.

        CLV sign conventions (match nba_bet_tracking.py):
          clvLine    > 0  → line moved in your favor after you bet (got better number)
          clvOddsPct > 0  → you beat the close (got better decimal odds)
        """
        player_name = (
            journal_entry.get("playerName")
            or journal_entry.get("playerIdentifierInput")
            or ""
        )
        stat     = journal_entry.get("stat", "")
        side     = journal_entry.get("recommendedSide", "over")
        bet_line = journal_entry.get("line") or journal_entry.get("lineAtBet")
        bet_odds = journal_entry.get("oddsAtBet") or journal_entry.get("recommendedOdds")
        book_hint = (
            journal_entry.get("bestOverBook") if side == "over"
            else journal_entry.get("bestUnderBook")
        )

        if not player_name or not stat:
            return {"success": False, "error": "missing player or stat in journal entry"}

        closing = (
            self.get_closing_line(date_str, player_name, stat, book_hint, phase="pregame")
            or self.get_closing_line(date_str, player_name, stat, phase="pregame")
        )
        if not closing:
            return {
                "success": False,
                "error": f"no closing line found for {player_name}/{stat} on {date_str}",
            }

        closing_line = closing.get("line")
        closing_odds = closing.get("over_odds") if side == "over" else closing.get("under_odds")

        clv_line = None
        if bet_line is not None and closing_line is not None:
            if side == "over":
                clv_line = round(_safe_float(closing_line) - _safe_float(bet_line), 2)
            else:
                clv_line = round(_safe_float(bet_line) - _safe_float(closing_line), 2)

        clv_odds_pct = None
        bet_dec   = _american_to_decimal(bet_odds)
        close_dec = _american_to_decimal(closing_odds)
        if bet_dec > 0 and close_dec > 0:
            clv_odds_pct = round((bet_dec / close_dec - 1.0) * 100.0, 2)

        return {
            "success":             True,
            "closingLine":         closing_line,
            "closingOdds":         closing_odds,
            "closingBook":         closing.get("book"),
            "closingTimestampUtc": closing.get("timestamp_utc"),
            "clvLine":             clv_line,
            "clvOddsPct":          clv_odds_pct,
        }

    def clv_summary_for_date(self, date_str: str, journal_path: str = None) -> dict:
        """
        Load all journal entries for date_str, compute CLV for each from stored
        line history, return aggregate summary + per-entry rows.
        """
        jpath = journal_path or _JOURNAL_PATH
        if not os.path.exists(jpath):
            return {"success": False, "error": "prop_journal.jsonl not found"}

        entries = []
        with open(jpath, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                pick_date = e.get("pickDate") or str(e.get("createdAtLocal", ""))[:10]
                if pick_date == date_str:
                    entries.append(e)

        if not entries:
            return {"success": True, "date": date_str, "sampleSize": 0,
                    "clvSampleSize": 0, "entries": []}

        rows = []
        for e in entries:
            clv = self.compute_clv(e, date_str)
            rows.append({
                "entryId":     e.get("entryId"),
                "playerName":  e.get("playerName"),
                "stat":        e.get("stat"),
                "side":        e.get("recommendedSide"),
                "betLine":     e.get("line"),
                "betOdds":     e.get("oddsAtBet"),
                "book":        e.get("bestOverBook") or e.get("bestUnderBook"),
                "closingLine": clv.get("closingLine"),
                "closingOdds": clv.get("closingOdds"),
                "clvLine":     clv.get("clvLine"),
                "clvOddsPct":  clv.get("clvOddsPct"),
                "clvSuccess":  clv.get("success"),
                "clvError":    clv.get("error"),
            })

        matched    = [r for r in rows if r["clvSuccess"]]
        clv_lines  = [r["clvLine"]    for r in matched if r["clvLine"]    is not None]
        clv_odds   = [r["clvOddsPct"] for r in matched if r["clvOddsPct"] is not None]

        def _pct_positive(vals):
            if not vals:
                return None
            return round(sum(1 for x in vals if x > 0) / len(vals) * 100, 1)

        return {
            "success":             True,
            "date":                date_str,
            "sampleSize":          len(entries),
            "clvSampleSize":       len(matched),
            "avgClvLine":          round(statistics.mean(clv_lines), 3) if clv_lines else None,
            "avgClvOddsPct":       round(statistics.mean(clv_odds),  3) if clv_odds  else None,
            "positiveClvLinePct":  _pct_positive(clv_lines),
            "positiveClvOddsPct":  _pct_positive(clv_odds),
            "entries":             rows,
        }

    # -----------------------------------------------------------------------
    # Stale-line detection
    # -----------------------------------------------------------------------

    def detect_stale_lines(self, date_str: str, min_line_diff: float = 0.5) -> list:
        """
        For each player/stat, compare each book's latest line against the median
        across all books.  Returns list of stale opportunities sorted by line_diff desc.

        A 'stale' book is one that hasn't moved its line to match consensus.
        direction='low'  → that book's line is lower than consensus → value on OVER
        direction='high' → that book's line is higher than consensus → value on UNDER
        """
        all_snaps = self.get_snapshots(date_str, phase="pregame")
        if not all_snaps:
            return []

        # Keep only the latest snapshot per (player_name, stat, book)
        latest = {}
        for s in sorted(all_snaps, key=lambda x: x.get("timestamp_utc", "")):
            key = (_normalize_name(s.get("player_name", "")), s.get("stat", ""), s.get("book", ""))
            latest[key] = s

        # Group by (player_name, stat)
        groups = defaultdict(list)
        for (pname, stat, _book), snap in latest.items():
            groups[(pname, stat)].append(snap)

        stale = []
        for (pname, stat), book_snaps in groups.items():
            if len(book_snaps) < 2:
                continue
            lines = [s["line"] for s in book_snaps if s.get("line") is not None]
            if len(lines) < 2:
                continue
            consensus = statistics.median(lines)
            for snap in book_snaps:
                line = snap.get("line")
                if line is None:
                    continue
                diff = abs(_safe_float(line) - consensus)
                if diff >= min_line_diff:
                    direction  = "low" if _safe_float(line) < consensus else "high"
                    value_side = "over" if direction == "low" else "under"
                    stale.append({
                        "player_name":      snap.get("player_name"),
                        "stat":             stat,
                        "stale_book":       snap.get("book"),
                        "stale_line":       line,
                        "consensus_line":   round(consensus, 2),
                        "line_diff":        round(diff, 2),
                        "direction":        direction,
                        "recommended_side": value_side,
                        "over_odds":        snap.get("over_odds"),
                        "under_odds":       snap.get("under_odds"),
                        "game_id":          snap.get("game_id"),
                        "player_team_abbr": snap.get("player_team_abbr", ""),
                        "opponent_abbr":    snap.get("opponent_abbr", ""),
                        "timestamp_utc":    snap.get("timestamp_utc"),
                        "reason_type":      "stale_line",
                    })

        return sorted(stale, key=lambda x: -x["line_diff"])

    # -----------------------------------------------------------------------
    # Alert store
    # -----------------------------------------------------------------------

    def append_alert(self, alert: dict, date_str: str = None) -> None:
        """Persist an alert dict to data/alerts/YYYY-MM-DD.jsonl."""
        ds   = date_str or str(alert.get("generated_at", _utc_now_iso()))[:10]
        path = os.path.join(ALERTS_DIR, f"{ds}.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(alert, default=str) + "\n")

    def get_alerts(self, date_str: str) -> list:
        path = os.path.join(ALERTS_DIR, f"{date_str}.jsonl")
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return rows

    # -----------------------------------------------------------------------
    # Injury snapshot store
    # -----------------------------------------------------------------------

    def load_injury_status(self) -> dict:
        """
        Load the rolling player-status dict from disk.
        Shape: { player_name_normalized: {status, confidence, team_abbr, last_seen_utc} }
        """
        path = os.path.join(INJURY_SNAP_DIR, "status.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def save_injury_status(self, status_map: dict) -> None:
        """Persist the rolling player-status dict."""
        path = os.path.join(INJURY_SNAP_DIR, "status.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(status_map, fh, indent=2, default=str)

    def diff_injury_status(self, new_signals: list, prev_map: dict) -> list:
        """
        Compare new injury signals against previous status map.
        Returns list of newly-triggered events (new OUT/Q/Doubtful not seen before).

        new_signals: list of {playerName, teamAbbr, status, confidence, source}
        prev_map:    { normalized_name: {status, ...} }
        Returns:     list of signals where status is newly detected or escalated
        """
        ESCALATION_RANK = {"Out": 4, "Doubtful": 3, "Questionable": 2,
                           "Minutes Watch": 1, "Probable": 0}
        ALERT_STATUSES  = {"Out", "Doubtful", "Questionable"}

        triggered = []
        for sig in new_signals:
            name_norm   = _normalize_name(sig.get("playerName", ""))
            new_status  = sig.get("status", "")
            new_conf    = _safe_float(sig.get("confidence", 0))
            if new_status not in ALERT_STATUSES:
                continue
            if new_conf < 0.55:   # require at least medium confidence
                continue
            prev = prev_map.get(name_norm)
            if prev is None:
                # Player not in our map at all → newly detected
                triggered.append(sig)
                continue
            prev_status = prev.get("status", "")
            prev_rank   = ESCALATION_RANK.get(prev_status, -1)
            new_rank    = ESCALATION_RANK.get(new_status, -1)
            if new_rank > prev_rank:
                # Status escalated (e.g. Probable → Out)
                triggered.append({**sig, "previousStatus": prev_status})

        return triggered
