#!/usr/bin/env python3
"""Bet journaling, settlement, daily reporting, and training export."""

import csv
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from nba_api.stats.endpoints import playergamelog
from nba_api.stats.static import players as nba_players_static

from .nba_data_collection import HEADERS, API_DELAY, retry_api_call, safe_round, safe_div, BETTING_POLICY, validate_player_team
from .nba_toon import to_toon_table, toon_print_section

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
JOURNAL_PATH = DATA_DIR / "prop_journal.jsonl"
LEAN_BETS_PATH = DATA_DIR / "lean_bets.jsonl"

_PLAYERS_BY_ID = {
    int(p["id"]): str(p["full_name"])
    for p in nba_players_static.get_players()
    if p.get("id")
}

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation-excluded books: these entries are kept in the journal for
# audit trail but must not pollute forward-validation metrics (hit rate,
# ROI, CLV).  Mirrors _VALIDATION_EXCLUDED_BOOKS in nba_decision_journal.
# ---------------------------------------------------------------------------
_VALIDATION_EXCLUDED_BOOKS = frozenset({"user_supplied"})


def _is_excluded_book_entry(entry):
    """Return True if entry's book source is in the exclusion set."""
    for field in ("bestOverBook", "bestUnderBook", "book"):
        val = str(entry.get(field) or "").strip().lower()
        if val in _VALIDATION_EXCLUDED_BOOKS:
            return True
    return False


# ---------------------------------------------------------------------------
# Write-time validation: flag obviously bad entries as quarantine
# ---------------------------------------------------------------------------

_LINE_CEILING = {
    "pts": 60, "pra": 60,
    "reb": 30, "ast": 30,
    "fg3m": 15, "stl": 15, "blk": 15, "tov": 15,
}

_CLOSING_LINE_MAX_DELTA = 8


def _validate_entry(entry):
    """Flag (but never reject) entries that look suspicious.

    Adds ``quarantine=True`` and ``quarantineReason`` to the entry dict
    *in-place* when a check fails.  Multiple reasons are joined with ``; ``.
    Returns the same entry dict for convenience.
    """
    reasons = []
    stat = str(entry.get("stat", "")).lower()
    line = _as_float(entry.get("line"))

    # 1. Team / player validity ------------------------------------------
    player_team = str(entry.get("playerTeamAbbr", "")).upper()
    opponent = str(entry.get("opponentAbbr", "")).upper()
    if player_team and opponent and player_team == opponent:
        reasons.append("team_equals_opponent")
        _log.warning(
            "quarantine: playerTeamAbbr (%s) == opponentAbbr for %s",
            player_team, entry.get("playerName", "?"),
        )

    # 2. Line sanity ------------------------------------------------------
    if line is not None:
        ceiling = _LINE_CEILING.get(stat)
        if ceiling is not None and abs(line) > ceiling:
            reasons.append(f"line_out_of_range ({stat} line={line}, max={ceiling})")
            _log.warning(
                "quarantine: %s line %.1f exceeds ceiling %d for %s",
                stat, line, ceiling, entry.get("playerName", "?"),
            )

    # 3. Closing-line delta -----------------------------------------------
    closing = _as_float(entry.get("closingLine"))
    if line is not None and closing is not None:
        delta = abs(closing - line)
        if delta >= _CLOSING_LINE_MAX_DELTA:
            reasons.append(
                f"closing_line_mismatch (line={line}, closing={closing}, delta={delta:.1f})"
            )
            _log.warning(
                "quarantine: closing-line delta %.1f for %s %s (line=%.1f, close=%.1f)",
                delta, entry.get("playerName", "?"), stat, line, closing,
            )

    if reasons:
        entry["quarantine"] = True
        existing = entry.get("quarantineReason", "")
        combined = "; ".join(reasons)
        entry["quarantineReason"] = (
            f"{existing}; {combined}" if existing else combined
        )

    return entry


def _now_utc_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _today_local_str():
    return datetime.now().date().isoformat()


def _game_date_from_utc(commence_time_str):
    """Convert Odds API commenceTime (UTC ISO) to NBA game date using UTC-6 offset.
    NBA games tip off 7-10 PM ET; UTC-6 keeps the correct calendar date.
    Returns None if parsing fails (caller must handle)."""
    try:
        s = str(commence_time_str or "").rstrip("Z").replace("T", " ")
        dt_utc = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return (dt_utc - timedelta(hours=6)).date().isoformat()
    except Exception:
        return None


def _yesterday_local_str():
    return (datetime.now().date() - timedelta(days=1)).isoformat()


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _as_float(val, default=None):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _as_int(val, default=None):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _season_from_date(date_obj):
    y = date_obj.year
    return f"{y}-{str(y + 1)[-2:]}" if date_obj.month >= 10 else f"{y - 1}-{str(y)[-2:]}"


def _parse_pick_date(date_str):
    return datetime.strptime(str(date_str), "%Y-%m-%d").date()


def _parse_game_date(raw):
    s = str(raw or "").strip()
    if not s:
        return None
    for candidate in (s, s.title()):
        for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _load_journal_entries():
    return _load_entries_from_path(JOURNAL_PATH)


def _load_entries_from_path(path):
    path = Path(path)
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _write_journal_entries(entries):
    for entry in entries:
        _validate_entry(entry)
    _write_entries_to_path(JOURNAL_PATH, entries)


def _write_entries_to_path(path, entries):
    _ensure_data_dir()
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="prop_journal_", suffix=".tmp", dir=str(target_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, separators=(",", ":"), ensure_ascii=False))
                f.write("\n")
        os.replace(tmp_path, target_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _append_journal_entry(entry):
    _ensure_data_dir()
    existing_entries = _load_journal_entries()
    new_key = _entry_key(entry)
    key_exists = any(_entry_key(existing) == new_key for existing in existing_entries)
    if key_exists:
        _write_journal_entries(_dedupe_latest(existing_entries + [entry]))
        return {"success": True, "isDuplicate": True, "journalPath": str(JOURNAL_PATH)}

    with JOURNAL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))
        f.write("\n")
    return {"success": True, "isDuplicate": False, "journalPath": str(JOURNAL_PATH)}


def _entry_key(entry):
    return (
        str(entry.get("pickDate", "")),
        _as_int(entry.get("playerId"), 0),
        str(entry.get("opponentAbbr", "")).upper(),
        bool(entry.get("isHome")),
        bool(entry.get("isB2B")),
        str(entry.get("stat", "")).lower(),
        safe_round(_as_float(entry.get("line"), 0.0), 3),
        _as_int(entry.get("overOdds"), 0),
        _as_int(entry.get("underOdds"), 0),
    )


def _dedupe_latest(entries):
    latest = {}
    for entry in entries:
        key = _entry_key(entry)
        ts = str(entry.get("createdAtUtc", ""))
        prev = latest.get(key)
        if prev is None or ts >= str(prev.get("createdAtUtc", "")):
            latest[key] = entry
    return list(latest.values())


def _cleanup_entry_key(entry):
    player_id = _as_int(entry.get("playerId"))
    if player_id:
        player_key = f"id:{player_id}"
    else:
        player_key = str(
            entry.get("playerName")
            or entry.get("playerIdentifierInput")
            or ""
        ).strip().lower()
    return (
        str(entry.get("pickDate", "")),
        player_key,
        str(entry.get("stat", "")).lower(),
        safe_round(_as_float(entry.get("line", entry.get("lineAtBet")), 0.0), 3),
    )


def dedup_journal(journal_path=None, write=True):
    target_path = Path(journal_path) if journal_path is not None else JOURNAL_PATH
    entries = _load_entries_from_path(target_path)
    latest = {}
    for entry in entries:
        key = _cleanup_entry_key(entry)
        created_at = str(entry.get("createdAtUtc", ""))
        previous = latest.get(key)
        if previous is None or created_at >= str(previous.get("createdAtUtc", "")):
            latest[key] = entry
    deduped = sorted(latest.values(), key=lambda item: str(item.get("createdAtUtc", "")))
    removed = len(entries) - len(deduped)
    if write and entries:
        _write_entries_to_path(target_path, deduped)
    return {
        "success": True,
        "journalPath": str(target_path),
        "beforeCount": len(entries),
        "afterCount": len(deduped),
        "removedCount": removed,
        "writeApplied": bool(write),
    }


def _resolve_recommended_side(ev_data):
    over = (ev_data or {}).get("over") or {}
    under = (ev_data or {}).get("under") or {}
    ev_over = _as_float(over.get("evPercent"))
    ev_under = _as_float(under.get("evPercent"))

    if ev_over is None and ev_under is None:
        return None
    if ev_under is None:
        return "over"
    if ev_over is None:
        return "under"
    return "over" if ev_over >= ev_under else "under"


def log_prop_ev_entry(
    prop_result,
    *,
    player_id,
    player_identifier,
    player_team_abbr,
    opponent_abbr,
    is_home,
    stat,
    line,
    over_odds,
    under_odds,
    is_b2b=False,
    source="cli",
):
    """
    Append a successful prop_ev evaluation into a local JSONL journal.
    """
    if not (prop_result or {}).get("success"):
        return {"success": False, "error": "prop_result is not successful"}

    # --- Team validation gate: reject phantom players ---
    _home_t = str(player_team_abbr or "").upper() if is_home else str(opponent_abbr or "").upper()
    _away_t = str(opponent_abbr or "").upper() if is_home else str(player_team_abbr or "").upper()
    _pname = _PLAYERS_BY_ID.get(int(player_id), str(player_identifier or ""))
    _actual_team, _team_valid = validate_player_team(
        _pname, str(player_team_abbr or ""), _home_t, _away_t,
    )
    if not _team_valid:
        return {
            "success": False,
            "error": f"phantom_player: {_pname} actual_team={_actual_team} "
                     f"not in event {_home_t}@{_away_t}",
        }
    player_team_abbr = _actual_team or player_team_abbr

    projection = (prop_result or {}).get("projection") or {}
    ev = (prop_result or {}).get("ev") or {}
    over = ev.get("over") or {}
    under = ev.get("under") or {}

    recommended_side = _resolve_recommended_side(ev)
    recommended_ev = _as_float((over if recommended_side == "over" else under).get("evPercent"), None)
    recommended_prob = _as_float((over if recommended_side == "over" else under).get("pSideNoPush"), None)
    effective_over_odds = _as_int((prop_result or {}).get("bestOverOdds"), _as_int(over_odds))
    effective_under_odds = _as_int((prop_result or {}).get("bestUnderOdds"), _as_int(under_odds))
    recommended_odds = effective_over_odds if recommended_side == "over" else effective_under_odds

    minutes_ctx = (prop_result or {}).get("minutesProjection") or {}
    minutes_cap_applied = minutes_ctx.get("minutesCapApplied")
    minutes_cap_reason = minutes_ctx.get("minutesCapReason")

    now_local = datetime.now().replace(microsecond=0).isoformat()
    # Strict: derive pickDate from commenceTime only. If missing, reject.
    _commence = (prop_result or {}).get("commenceTime")
    if not _commence:
        return {"success": False, "error": "missing_commence_time: cannot determine game date"}
    _pick_date = _game_date_from_utc(_commence)
    if not _pick_date:
        return {"success": False, "error": "bad_commence_time: cannot parse game date"}
    entry = {
        "entryId": str(uuid.uuid4()),
        "createdAtUtc": _now_utc_iso(),
        "createdAtLocal": now_local,
        "pickDate": _pick_date,
        "source": str(source or "cli"),
        "playerId": int(player_id),
        "playerName": _PLAYERS_BY_ID.get(int(player_id), ""),
        "playerIdentifierInput": str(player_identifier or ""),
        "playerTeamAbbr": str(player_team_abbr or "").upper(),
        "opponentAbbr": str(opponent_abbr or "").upper(),
        "isHome": bool(is_home),
        "isB2B": bool(is_b2b),
        "stat": str(stat or "").lower(),
        "line": _as_float(line),
        "lineAtBet": _as_float(line),
        "overOdds": effective_over_odds,
        "underOdds": effective_under_odds,
        "bestOverBook": (prop_result or {}).get("bestOverBook"),
        "bestUnderBook": (prop_result or {}).get("bestUnderBook"),
        "projection": _as_float(projection.get("projection")),
        "projStdev": _as_float(projection.get("projStdev") or projection.get("stdev")),
        "probOver": _as_float(ev.get("probOver")),
        "probUnder": _as_float(ev.get("probUnder")),
        "probPush": _as_float(ev.get("probPush"), 0.0),
        "distributionMode": str(ev.get("distributionMode", "normal")),
        "confidence": _as_float(projection.get("confidence")),
        "evOverPct": _as_float(over.get("evPercent")),
        "evUnderPct": _as_float(under.get("evPercent")),
        "overVerdict": str(over.get("verdict", "")),
        "underVerdict": str(under.get("verdict", "")),
        "recommendedSide": recommended_side,
        "recommendedEvPct": recommended_ev,
        "recommendedProbNoPush": recommended_prob,
        "recommendedOdds": recommended_odds,
        "oddsAtBet": recommended_odds,
        "closingLine": None,
        "closingOdds": None,
        "clvLine": None,
        "clvOddsPct": None,
        "clvComputedAtUtc": None,
        "settled": False,
        "settlementStatus": "pending",
        "settledAtUtc": None,
        "actualStat": None,
        "actualGameDate": None,
        "actualMatchup": None,
        "overOutcome": None,
        "underOutcome": None,
        "result": None,
        "pnl1u": None,
    }
    if minutes_cap_applied is not None:
        entry["minutesCapApplied"] = bool(minutes_cap_applied)
    if minutes_cap_reason is not None:
        entry["minutesCapReason"] = str(minutes_cap_reason)

    _validate_entry(entry)

    try:
        append_result = _append_journal_entry(entry)
        return {
            "success": True,
            "entryId": entry["entryId"],
            "journalPath": str(JOURNAL_PATH),
            "isDuplicate": bool(append_result.get("isDuplicate")),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _fetch_player_logs(player_id, season):
    def fetch():
        return playergamelog.PlayerGameLog(
            player_id=player_id,
            season=season,
            season_type_all_star="Regular Season",
            headers=HEADERS,
            timeout=30,
        )

    return retry_api_call(fetch).get_normalized_dict().get("PlayerGameLog", [])


def _find_game_row(logs, target_date, opponent_abbr=None, is_home=None):
    """Find player game-log row for a specific date.

    When opponent_abbr or is_home are provided they are **hard** filters —
    if no row matches the supplied opponent/home, return None instead of
    falling back to the first same-date row.  This prevents grading a bet
    against the wrong game (e.g. after a trade or on a doubleheader date).
    """
    candidates = []
    for row in logs:
        game_date = _parse_game_date(row.get("GAME_DATE"))
        if game_date != target_date:
            continue
        matchup = str(row.get("MATCHUP", "") or "")
        opp = matchup.split(" ")[-1] if matchup else ""
        home_flag = "vs." in matchup
        candidates.append((row, opp, home_flag))

    if opponent_abbr:
        candidates = [x for x in candidates if str(x[1]).upper() == str(opponent_abbr).upper()]

    if is_home is not None:
        candidates = [x for x in candidates if bool(x[2]) == bool(is_home)]

    return candidates[0][0] if candidates else None


def _extract_stat_from_row(row, stat):
    """Extract a stat value from a game-log row.

    Returns None (not 0.0) when a component field is missing, non-numeric, or
    outside sane bounds — prevents grading on corrupted NBA API data.
    """
    _MAX_STAT = {"PTS": 100, "REB": 40, "AST": 35, "STL": 15, "BLK": 15, "TOV": 20, "FG3M": 20}

    def _safe(key):
        v = _as_float(row.get(key))
        if v is None or v < 0 or v > _MAX_STAT.get(key, 100):
            return None
        return v

    pts = _safe("PTS")
    reb = _safe("REB")
    ast = _safe("AST")
    stl = _safe("STL")
    blk = _safe("BLK")
    tov = _safe("TOV")
    fg3m = _safe("FG3M")

    stat_key = str(stat or "").lower()
    mapping = {
        "pts": pts,
        "reb": reb,
        "ast": ast,
        "stl": stl,
        "blk": blk,
        "tov": tov,
        "fg3m": fg3m,
    }
    # Combo stats: require all components to be valid
    if stat_key == "pra":
        return (pts + reb + ast) if pts is not None and reb is not None and ast is not None else None
    if stat_key == "pr":
        return (pts + reb) if pts is not None and reb is not None else None
    if stat_key == "pa":
        return (pts + ast) if pts is not None and ast is not None else None
    if stat_key == "ra":
        return (reb + ast) if reb is not None and ast is not None else None

    return mapping.get(stat_key)


def _grade_side(actual, line, side):
    actual_val = _as_float(actual)
    line_val = _as_float(line)
    if actual_val is None or line_val is None:
        return "ungraded"
    if abs(actual_val - line_val) < 1e-9:
        return "push"
    if str(side).lower() == "over":
        return "win" if actual_val > line_val else "loss"
    if str(side).lower() == "under":
        return "win" if actual_val < line_val else "loss"
    return "ungraded"


def _profit_from_american_odds(odds):
    o = _as_float(odds)
    if o is None or o == 0:
        return None
    return (o / 100.0) if o > 0 else (100.0 / abs(o))


def _pnl_for_outcome(result, odds):
    if result == "win":
        win_profit = _profit_from_american_odds(odds)
        return safe_round(win_profit, 4) if win_profit is not None else None
    if result == "loss":
        return -1.0
    if result == "push":
        return 0.0
    return None


def _summarize_clv(entries):
    """Compute CLV aggregate metrics over entries that have closing line data."""
    clv_entries = [e for e in entries if e.get("clvLine") is not None]
    n = len(clv_entries)
    if n == 0:
        return {
            "clvSampleSize": 0,
            "avgClvLine": None,
            "avgClvOddsPct": None,
            "positiveClvCount": None,
            "positiveClvPct": None,
        }
    avg_clv_line = safe_round(
        sum(_as_float(e.get("clvLine"), 0.0) for e in clv_entries) / n, 4
    )
    odds_entries = [e for e in clv_entries if e.get("clvOddsPct") is not None]
    avg_clv_odds = (
        safe_round(
            sum(_as_float(e.get("clvOddsPct"), 0.0) for e in odds_entries) / len(odds_entries), 4
        )
        if odds_entries
        else None
    )
    positive_count = sum(
        1 for e in clv_entries if (_as_float(e.get("clvLine"), 0.0) or 0.0) > 0
    )
    positive_pct = safe_round(positive_count / n * 100.0, 2)
    return {
        "clvSampleSize": n,
        "avgClvLine": avg_clv_line,
        "avgClvOddsPct": avg_clv_odds,
        "positiveClvCount": positive_count,
        "positiveClvPct": positive_pct,
    }


def _summarize_settled(entries):
    graded = [e for e in entries if str(e.get("result")) in {"win", "loss", "push"}]
    wins = sum(1 for e in graded if e.get("result") == "win")
    losses = sum(1 for e in graded if e.get("result") == "loss")
    pushes = sum(1 for e in graded if e.get("result") == "push")
    risk_non_push = wins + losses
    total_bets = len(graded)
    hit_rate = safe_round(safe_div(wins, risk_non_push, default=0.0) * 100.0, 2) if risk_non_push > 0 else None
    total_pnl = safe_round(sum(_as_float(e.get("pnl1u"), 0.0) for e in graded), 4)
    roi = safe_round(safe_div(total_pnl, total_bets, default=0.0) * 100.0, 2) if total_bets > 0 else None
    return {
        "gradedCount": total_bets,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "hitRateNoPushPct": hit_rate,
        "pnlUnits": total_pnl,
        "roiPctPerBet": roi,
    }


def settle_entries_for_date(date_str):
    """
    Settle all un-settled journal entries for a date (YYYY-MM-DD).
    """
    try:
        target_date = _parse_pick_date(date_str)
    except ValueError:
        return {"success": False, "error": "Invalid date format. Use YYYY-MM-DD.", "date": str(date_str)}
    entries = _load_journal_entries()
    candidates = [
        e for e in entries
        if str(e.get("pickDate")) == str(date_str)
        and not bool(e.get("settled"))
    ]
    if not candidates:
        return {
            "success": True,
            "date": str(date_str),
            "message": "No pending entries for this date.",
            "pendingCount": 0,
            "settledNow": 0,
            "summary": _summarize_settled([e for e in entries if str(e.get("pickDate")) == str(date_str) and not _is_excluded_book_entry(e)]),
        }

    logs_cache = {}
    touched = 0
    unresolved = 0

    for entry in entries:
        if str(entry.get("pickDate")) != str(date_str) or bool(entry.get("settled")):
            continue

        player_id = _as_int(entry.get("playerId"), 0)
        if player_id <= 0:
            entry["settlementStatus"] = "pending_data"
            entry["lastSettlementAttemptUtc"] = _now_utc_iso()
            unresolved += 1
            continue

        season = _season_from_date(target_date)
        cache_key = (player_id, season)
        if cache_key not in logs_cache:
            if logs_cache:
                time.sleep(API_DELAY)
            try:
                logs_cache[cache_key] = _fetch_player_logs(player_id, season)
            except Exception:
                logs_cache[cache_key] = []

        row = _find_game_row(
            logs_cache[cache_key],
            target_date,
            opponent_abbr=entry.get("opponentAbbr"),
            is_home=entry.get("isHome"),
        )
        if not row:
            # Phantom detection: if player's actual team is known and not in event, void it
            _pt = str(entry.get("playerTeamAbbr") or "").upper()
            _oa = str(entry.get("opponentAbbr") or "").upper()
            _ht = _pt if entry.get("isHome") else _oa
            _at = _oa if entry.get("isHome") else _pt
            _act, _valid = validate_player_team(
                entry.get("playerName", ""), _pt, _ht, _at,
            )
            if not _valid and _act:
                entry["settled"] = True
                entry["settlementStatus"] = "phantom_no_game"
                entry["result"] = "void"
                entry["pnl1u"] = 0.0
                entry["settledAtUtc"] = _now_utc_iso()
                entry["phantomActualTeam"] = _act
                touched += 1
                continue
            entry["settlementStatus"] = "pending_data"
            entry["lastSettlementAttemptUtc"] = _now_utc_iso()
            unresolved += 1
            continue

        # Safety: PlayerGameLog normally only returns completed-game rows,
        # but verify MIN field is present as belt-and-suspenders for final status.
        if "MIN" not in row and "min" not in row:
            entry["settlementStatus"] = "pending_data"
            entry["lastSettlementAttemptUtc"] = _now_utc_iso()
            unresolved += 1
            continue

        actual_stat = _extract_stat_from_row(row, entry.get("stat"))
        if actual_stat is None:
            entry["settlementStatus"] = "unsupported_stat"
            entry["lastSettlementAttemptUtc"] = _now_utc_iso()
            unresolved += 1
            continue

        line = _as_float(entry.get("line"))
        over_result = _grade_side(actual_stat, line, "over")
        under_result = _grade_side(actual_stat, line, "under")

        side = str(entry.get("recommendedSide") or "").lower()
        if side == "over":
            final_result = over_result
            final_odds = entry.get("overOdds")
        elif side == "under":
            final_result = under_result
            final_odds = entry.get("underOdds")
        else:
            final_result = "ungraded"
            final_odds = None

        entry["settled"] = final_result in {"win", "loss", "push"}
        entry["settlementStatus"] = final_result
        entry["settledAtUtc"] = _now_utc_iso() if entry["settled"] else None
        entry["actualStat"] = safe_round(actual_stat, 3)
        entry["actualGameDate"] = target_date.isoformat()
        entry["actualMatchup"] = str(row.get("MATCHUP", ""))
        entry["overOutcome"] = over_result
        entry["underOutcome"] = under_result
        entry["result"] = final_result
        entry["pnl1u"] = _pnl_for_outcome(final_result, final_odds)
        touched += 1

    # Dedupe the full journal on settlement to prevent accumulation of stale duplicates
    deduped_entries = _dedupe_latest(entries)
    _write_journal_entries(deduped_entries)

    same_day_entries = [e for e in deduped_entries if str(e.get("pickDate")) == str(date_str)]
    same_day_deduped = _dedupe_latest(same_day_entries)
    # Exclude user_supplied entries from validation metrics
    metric_deduped = [e for e in same_day_deduped if not _is_excluded_book_entry(e)]
    summary = _summarize_settled(metric_deduped)
    summary.update(_summarize_clv(metric_deduped))
    return {
        "success": True,
        "date": str(date_str),
        "pendingCount": len(candidates),
        "settledNow": touched,
        "unresolved": unresolved,
        "entriesLogged": len(same_day_entries),
        "entriesUnique": len(same_day_deduped),
        "summary": summary,
    }


def settle_yesterday():
    return settle_entries_for_date(_yesterday_local_str())


def auto_settle_today():
    """Settle any finished games for today + yesterday (catches late-night UTC overlap).

    Settles BOTH journal entries (prop_journal.jsonl) AND decision-journal leans
    (lean_outcomes in SQLite). Called on page refresh so the UI shows settled
    results for completed games.
    """
    today = _today_local_str()
    yesterday = _yesterday_local_str()
    settled_journal = 0
    settled_leans = 0
    errors = []

    # 1. Settle journal entries (prop_journal.jsonl)
    for d in (today, yesterday):
        try:
            r = settle_entries_for_date(d)
            settled_journal += r.get("settledNow", 0)
        except Exception as e:
            errors.append(f"journal {d}: {e}")

    # 2. Settle decision-journal leans (SQLite lean_outcomes)
    try:
        from .nba_decision_journal import DecisionJournal
        dj = DecisionJournal()
        for d in (today, yesterday):
            try:
                r = dj.settle_leans_for_date(d)
                settled_leans += r.get("settled", 0)
            except Exception as e:
                errors.append(f"leans {d}: {e}")
    except Exception as e:
        errors.append(f"leans init: {e}")

    return {
        "success": True,
        "settledNow": settled_journal + settled_leans,
        "settledJournal": settled_journal,
        "settledLeans": settled_leans,
        "dates": [today, yesterday],
        "errors": errors or None,
    }


def _load_line_history(date_str):
    """Load line-history JSONL for date_str → {(player_name_lower, stat_lower): [snapshots]}."""
    import json as _json
    path = DATA_DIR / "line_history" / f"{date_str}.jsonl"
    if not path.exists():
        return {}
    lookup = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = _json.loads(raw)
                except Exception:
                    continue
                name = str(row.get("player_name") or "").lower()
                stat = str(row.get("stat") or "").lower()
                if name and stat:
                    lookup.setdefault((name, stat), []).append(row)
    except Exception:
        return {}
    for key in lookup:
        lookup[key].sort(key=lambda r: str(r.get("timestamp_utc") or ""))
    return lookup


def _norm_pulled_name(n: str) -> str:
    """Normalize player name for pulled-lines matching (strip periods, Jr/Sr suffixes, lowercase)."""
    import re
    return re.sub(r"[.\-'']", "", str(n)).lower().strip()


def _get_pulled_players(target_date: str) -> set:
    """
    Return set of normalized player names whose lines were pulled today.

    Reads LineStore snapshots, groups by 10-min batch, and identifies
    players present in earlier full-sweep batches but absent from the
    latest one (same logic as roster_sweep pulled-lines detection).
    """
    try:
        from .nba_line_store import LineStore
        store = LineStore()
        snaps = store.get_snapshots(target_date)
        if not snaps:
            return set()

        _MIN_FULL_SWEEP = 50
        batch_players: dict[str, set] = {}
        for snap in snaps:
            if (snap.get("book") or "").lower() == "pinnacle":
                continue
            ts = (snap.get("timestamp_utc") or "")[:16]
            name = _norm_pulled_name(snap.get("player_name") or "")
            if ts and name:
                batch_players.setdefault(ts, set()).add(name)

        full_batches = {k: v for k, v in batch_players.items() if len(v) >= _MIN_FULL_SWEEP}
        if len(full_batches) < 2:
            return set()

        sorted_keys = sorted(full_batches.keys())
        latest = full_batches[sorted_keys[-1]]
        earlier: set = set()
        for bk in sorted_keys[:-1]:
            earlier |= full_batches[bk]

        return earlier - latest
    except Exception as exc:
        _log.debug("Pulled-lines check skipped: %s", exc)
        return set()


def _get_playing_teams_today(target_date=None):
    """Return set of uppercase team abbreviations with a game on target_date (default today)."""
    target = str(target_date or _today_local_str())
    try:
        from .nba_data_collection import get_todays_games
        result = get_todays_games(game_date=target)
        teams = set()
        for g in result.get("games", []):
            h = g.get("homeTeam", {}).get("abbreviation", "")
            a = g.get("awayTeam", {}).get("abbreviation", "")
            if h:
                teams.add(h.upper())
            if a:
                teams.add(a.upper())
        if teams:
            return teams
    except Exception:
        pass

    try:
        line_history = _load_line_history(target)
        teams = set()
        for snaps in (line_history or {}).values():
            for snap in snaps or []:
                for field in ("home_team_abbr", "away_team_abbr", "player_team_abbr", "opponent_abbr"):
                    team = str((snap or {}).get(field) or "").upper()
                    if team:
                        teams.add(team)
        if teams:
            return teams
    except Exception:
        pass

    return None  # graceful fallback — skip filter


def _sqlite_fallback_entries(target):
    """Fallback: read today's signals from DecisionJournal SQLite when JSONL is empty."""
    try:
        from .nba_decision_journal import DecisionJournal, _ct_day_utc_bounds
        with DecisionJournal() as dj:
            utc_start, utc_end = _ct_day_utc_bounds(target)
            cur = dj._conn.execute(
                """SELECT player_id, player_name, team_abbr, opponent_abbr,
                          stat, line, book, over_odds, under_odds,
                          projection, prob_over, prob_under,
                          edge_over, edge_under, recommended_side, recommended_edge
                   FROM signals
                   WHERE ts_utc >= ? AND ts_utc < ?
                   ORDER BY ts_utc DESC
                   LIMIT 500""",
                (utc_start, utc_end),
            )
            cols = [
                "player_id", "player_name", "team_abbr", "opponent_abbr",
                "stat", "line", "book", "over_odds", "under_odds",
                "projection", "prob_over", "prob_under",
                "edge_over", "edge_under", "recommended_side", "recommended_edge",
            ]
            entries = []
            for row in cur.fetchall():
                sig = dict(zip(cols, row))
                rec_side = str(sig.get("recommended_side") or "").lower()
                rec_edge = _as_float(sig.get("recommended_edge"), 0.0)
                over_odds = _as_int(sig.get("over_odds"))
                under_odds = _as_int(sig.get("under_odds"))
                entries.append({
                    "pickDate": target,
                    "playerId": _as_int(sig.get("player_id")),
                    "playerName": sig.get("player_name", ""),
                    "playerTeamAbbr": str(sig.get("team_abbr") or "").upper(),
                    "opponentAbbr": str(sig.get("opponent_abbr") or "").upper(),
                    "stat": str(sig.get("stat") or "").lower(),
                    "line": _as_float(sig.get("line")),
                    "overOdds": over_odds,
                    "underOdds": under_odds,
                    "recommendedSide": rec_side,
                    "recommendedEvPct": safe_round(rec_edge * 100.0, 2) if rec_edge else 0.0,
                    "projection": _as_float(sig.get("projection")),
                    "recommendedOdds": over_odds if rec_side == "over" else under_odds,
                    "probOver": _as_float(sig.get("prob_over")),
                    "probUnder": _as_float(sig.get("prob_under")),
                    "bestOverBook": sig.get("book") if rec_side == "over" else None,
                    "bestUnderBook": sig.get("book") if rec_side == "under" else None,
                    "settled": False,
                    "source": "sqlite_fallback",
                })
            return entries
    except Exception:
        return []


def best_plays_for_date(date_str=None, limit=15, unique_props=True):
    from .nba_model_ml_training import DEFAULT_OUTCOME_ML_MODEL_PATH, score_rows_with_outcome_ml

    target = str(date_str or _today_local_str())
    entries = _load_journal_entries()
    filtered = [e for e in entries if str(e.get("pickDate")) == target]
    sqlite_entries = _sqlite_fallback_entries(target)
    deduped = _dedupe_latest(filtered + sqlite_entries)

    # Filter out phantom signals: players whose teams aren't playing today
    playing_teams = _get_playing_teams_today(target_date=target)
    if playing_teams is not None:
        valid = []
        for e in deduped:
            team = str(e.get("playerTeamAbbr") or e.get("teamAbbr") or "").upper()
            opp = str(e.get("opponentAbbr") or "").upper()
            if team and team in playing_teams:
                valid.append(e)
            elif opp and opp in playing_teams:
                valid.append(e)
            # else: phantom signal — team not playing today, skip
        deduped = valid

    # Filter out players whose lines were pulled (injury/OUT)
    # Same logic as roster_sweep: if a player appeared in an earlier
    # full-sweep batch but is absent from the latest batch, they're OUT.
    pulled_names = _get_pulled_players(target)
    if pulled_names:
        before = len(deduped)
        deduped = [
            e for e in deduped
            if _norm_pulled_name(e.get("playerName") or "") not in pulled_names
        ]
        if len(deduped) < before:
            _log.info("Pulled-lines filter removed %d entries (%s)",
                       before - len(deduped),
                       ", ".join(sorted(pulled_names)[:5]))

    ranked = []
    for e in deduped:
        ev_pct = _as_float(e.get("recommendedEvPct"))
        if ev_pct is None:
            continue
        ranked.append(e)

    ranked.sort(
        key=lambda x: (_as_float(x.get("recommendedEvPct"), -1e9), str(x.get("createdAtUtc", ""))),
        reverse=True,
    )

    # One row per (player, stat, line, side): keep highest EV
    if unique_props:
        seen_prop = set()
        unique_ranked = []
        for e in ranked:
            prop_key = (
                _as_int(e.get("playerId"), 0),
                str(e.get("stat", "")).lower(),
                safe_round(_as_float(e.get("line"), 0.0), 3),
                str(e.get("recommendedSide", "over")).lower(),
            )
            if prop_key in seen_prop:
                continue
            seen_prop.add(prop_key)
            unique_ranked.append(e)
        ranked = unique_ranked

    limit_val = max(1, _as_int(limit, 15))
    top = ranked[:limit_val]
    _bp = BETTING_POLICY
    _wl = _bp.get("stat_whitelist", set())
    _bb = _bp.get("blocked_prob_bins", set())

    def _policy_check(e):
        """Return None if policy-qualified, else a short reason string."""
        stat = str(e.get("stat", "")).lower()
        if _wl and stat not in _wl:
            return f"{stat} not in whitelist"
        po = _as_float(e.get("probOver"), 0.5)
        bin_idx = max(0, min(9, int(po * 10)))
        if bin_idx in _bb:
            return f"bin {bin_idx} blocked ({bin_idx*10}-{(bin_idx+1)*10}%)"
        return None

    top_rows = [
        {
            "entryId": e.get("entryId"),
            "createdAtLocal": e.get("createdAtLocal"),
            "playerId": e.get("playerId"),
            "playerName": e.get("playerName"),
            "playerTeamAbbr": e.get("playerTeamAbbr"),
            "stat": e.get("stat"),
            "line": e.get("line"),
            "opponentAbbr": e.get("opponentAbbr"),
            "isHome": e.get("isHome"),
            "recommendedSide": e.get("recommendedSide"),
            "recommendedEvPct": e.get("recommendedEvPct"),
            "projection": e.get("projection"),
            "recommendedOdds": e.get("recommendedOdds"),
            "overOdds": e.get("overOdds"),
            "underOdds": e.get("underOdds"),
            "probOver": e.get("probOver"),
            "probUnder": e.get("probUnder"),
            "probBin": max(0, min(9, int(float(e.get("probOver") or 0.5) * 10))),
            "bestOverBook": e.get("bestOverBook"),
            "bestUnderBook": e.get("bestUnderBook"),
            "distributionMode": e.get("distributionMode"),
            "confidence": e.get("confidence"),
            "settled": e.get("settled"),
            "result": e.get("result"),
            "policyQualified": _policy_check(e) is None,
            "policyRejectReason": _policy_check(e),
        }
        for e in top
    ]
    for row, e in zip(top_rows, top):
        if e.get("minutesCapApplied") is not None:
            row["minutesCapApplied"] = bool(e["minutesCapApplied"])
        if e.get("minutesCapReason") is not None:
            row["minutesCapReason"] = str(e["minutesCapReason"])

    # Enrich with line movement from today's collected snapshots
    line_history = _load_line_history(target)
    if line_history:
        for row in top_rows:
            p_name = str(row.get("playerName") or "").lower()
            stat   = str(row.get("stat") or "").lower()
            snaps  = line_history.get((p_name, stat))
            # Fallback: last-name partial match
            if not snaps and p_name:
                last = p_name.strip().split()[-1]
                if len(last) > 2:
                    for (n, s), v in line_history.items():
                        if s == stat and last in n:
                            snaps = v
                            break
            if snaps:
                # Fix 3: Side-aware book selection + book+line filtering
                rec_side = str(row.get("recommendedSide") or "").lower()
                if rec_side == "over":
                    row_book = str(row.get("bestOverBook") or "").lower()
                elif rec_side == "under":
                    row_book = str(row.get("bestUnderBook") or "").lower()
                else:
                    row_book = str(row.get("bestOverBook") or row.get("bestUnderBook") or "").lower()
                row_line = _as_float(row.get("line"))

                # Three-level cascade: book+line → book-only → unfiltered
                use_snaps = snaps
                if row_book:
                    book_line_snaps = [
                        s for s in snaps
                        if str(s.get("book") or "").lower() == row_book
                        and (row_line is None or _as_float(s.get("line")) == row_line)
                    ]
                    if not book_line_snaps:
                        book_line_snaps = [
                            s for s in snaps
                            if str(s.get("book") or "").lower() == row_book
                        ]
                    if book_line_snaps:
                        use_snaps = book_line_snaps

                open_line = _as_float(use_snaps[0].get("line"))
                curr_line = _as_float(use_snaps[-1].get("line"))
                if open_line is not None and curr_line is not None:
                    delta = safe_round(curr_line - open_line, 2)
                    side  = str(row.get("recommendedSide") or "").lower()
                    if delta == 0:
                        favorable = None          # flat — no movement
                    elif side == "over":
                        favorable = delta > 0     # line rose = market agrees with over = positive CLV direction
                    elif side == "under":
                        favorable = delta < 0     # line dropped = market agrees with under = positive CLV direction
                    else:
                        favorable = None
                    row["lineMovement"] = {
                        "openLine":      open_line,
                        "currentLine":   curr_line,
                        "lineDelta":     delta,
                        "favorable":     favorable,
                        "snapshotCount": len(use_snaps),
                    }

    # #3/#9: Line movement conflict — raise effective edge threshold to 0.10 when
    # market moved AGAINST the recommended side (conflicting signal from sharp money).
    _conflict_threshold = 0.10
    for row in top_rows:
        lm = row.get("lineMovement")
        if lm is None:
            continue
        favorable = lm.get("favorable")
        if favorable is False:
            ev_pct = _as_float(row.get("recommendedEvPct"), 0.0) or 0.0
            row["lineMovementConflict"] = True
            row["lineMovementQualified"] = ev_pct >= _conflict_threshold
        elif favorable is True:
            row["lineMovementConflict"] = False
            row["lineMovementQualified"] = True
        # favorable is None (flat/no movement) → no annotation added

    # Sort: CLV-confirmed (conflict=False) first, neutral second, conflicted last.
    # Within each tier the existing EV rank is preserved (sort is stable).
    def _clv_tier(row):
        conflict = row.get("lineMovementConflict")
        if conflict is False:
            return 0   # confirmed — line moved with the bet
        if conflict is True:
            return 2   # conflicted — market moved against the bet
        return 1       # no movement data

    top_rows.sort(key=_clv_tier)

    outcome_model_meta = {"loaded": False}
    score_result = score_rows_with_outcome_ml(top_rows, model_path=DEFAULT_OUTCOME_ML_MODEL_PATH)
    if score_result.get("success"):
        top_rows = score_result.get("rows") or top_rows
        outcome_model_meta = {
            "loaded": bool(score_result.get("loaded")),
            "modelPath": score_result.get("modelPath"),
            "modelType": score_result.get("modelType"),
            "filterStats": score_result.get("filterStats"),
            "classWeightBalance": score_result.get("classWeightBalance"),
        }
    elif score_result.get("error") and "not found" not in str(score_result.get("error")).lower():
        outcome_model_meta = {
            "loaded": False,
            "error": score_result.get("error"),
        }

    positive_edges = sum(1 for e in ranked if (_as_float(e.get("recommendedEvPct"), 0.0) or 0.0) > 0)
    policy_qualified = [r for r in top_rows if r.get("policyQualified")]

    # --- Human-readable ranking (printed before the final JSON line) ---
    if sys.stdout.isatty() and top_rows:
        toon_rows = []
        for idx, row in enumerate(top_rows, 1):
            lm    = row.get("lineMovement") or {}
            delta = lm.get("lineDelta")
            fav   = lm.get("favorable")
            toon_rows.append({
                "#": idx,
                "player": str(row.get("playerName", "") or "").encode("ascii", "replace").decode("ascii"),
                "stat": row.get("stat", ""),
                "side": row.get("recommendedSide", ""),
                "line": row.get("line", 0),
                "evPct": safe_round(_as_float(row.get("recommendedEvPct"), 0.0) or 0.0, 1),
                "proj": safe_round(_as_float(row.get("projection"), 0.0) or 0.0, 1),
                "odds": row.get("recommendedOdds", ""),
                "move": f"{delta:+.1f}" if delta is not None else "-",
                "clv": "yes" if fav is True else ("no" if fav is False else "-"),
                "policy": "OK" if row.get("policyQualified") else (row.get("policyRejectReason") or "--"),
            })
        toon_print_section(
            f"BEST TODAY  {target}  ({len(ranked)} ranked | {len(policy_qualified)} policy-qualified)",
            to_toon_table(toon_rows, ["#", "player", "stat", "side", "line", "evPct", "proj", "odds", "move", "clv", "policy"]),
        )

    # Load model leans for the target date from lean_bets.jsonl
    model_leans = _load_leans_for_date(target, limit=limit_val, include_outcomes=True)
    lean_score_result = score_rows_with_outcome_ml(model_leans, model_path=DEFAULT_OUTCOME_ML_MODEL_PATH)
    if lean_score_result.get("success"):
        model_leans = lean_score_result.get("rows") or model_leans

    return {
        "success": True,
        "date": target,
        "totalRanked": len(ranked),
        "positiveEdgeCount": positive_edges,
        "entriesLogged": len(top_rows),
        "outcomeModel": outcome_model_meta,
        "policyQualified": policy_qualified,
        "topOffers": top_rows,
        "modelLeans": model_leans,
    }


def _load_leans_for_date(target_date: str, limit: int = 50, include_outcomes: bool = False) -> list:
    """Load model leans from decision_journal.sqlite leans table for a specific date."""
    import sqlite3
    from .nba_decision_journal import _ct_day_utc_bounds
    db_path = DATA_DIR / "decision_journal" / "decision_journal.sqlite"
    if not db_path.exists():
        return []
    try:
        utc_start, utc_end = _ct_day_utc_bounds(target_date)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        if include_outcomes:
            cur = conn.execute(
                """SELECT l.player_id, l.player_name, l.stat, l.line, l.book,
                          l.over_odds, l.under_odds, l.projection, l.prob_over, l.prob_under,
                          l.edge_over, l.edge_under, l.recommended_side, l.recommended_edge,
                          l.confidence, l.skip_reason,
                          lo.result, lo.pnl_units, lo.actual_stat
                   FROM leans l
                   LEFT JOIN lean_outcomes lo ON l.lean_id = lo.lean_id
                   WHERE l.ts_utc >= ? AND l.ts_utc < ?
                   ORDER BY l.recommended_edge DESC
                   LIMIT ?""",
                (utc_start, utc_end, limit),
            )
        else:
            cur = conn.execute(
                """SELECT player_id, player_name, stat, line, book,
                          over_odds, under_odds, projection, prob_over, prob_under,
                          edge_over, edge_under, recommended_side, recommended_edge,
                          confidence, skip_reason
                   FROM leans
                   WHERE ts_utc >= ? AND ts_utc < ?
                   ORDER BY recommended_edge DESC
                   LIMIT ?""",
                (utc_start, utc_end, limit),
            )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return []

    leans = []
    for r in rows:
        edge = r["recommended_edge"] or 0
        entry = {
            "playerName": r["player_name"],
            "playerId": r["player_id"],
            "stat": r["stat"],
            "line": r["line"],
            "projection": r["projection"],
            "probOver": r["prob_over"],
            "bin": max(0, min(9, int((r["prob_over"] or 0.5) * 10))),
            "recommendedSide": r["recommended_side"],
            "edge": safe_round(edge, 4),
            "recommendedEvPct": safe_round(edge * 100, 2),
            "recommendedOdds": r["over_odds"] if (r["recommended_side"] or "").lower() == "over" else r["under_odds"],
            "book": r["book"],
            "policyPass": False,
            "policyRejectReason": r["skip_reason"],
        }
        if include_outcomes:
            entry["result"] = r["result"]
            entry["settled"] = r["result"] is not None
            entry["pnl"] = r["pnl_units"]
            entry["actual"] = r["actual_stat"]
        leans.append(entry)

    # Deduplicate by (player, stat, line, side) — keep the entry with the highest edge
    seen: dict[tuple, dict] = {}
    for e in leans:
        key = (e["playerId"], e["stat"], e["line"], e["recommendedSide"])
        prev = seen.get(key)
        if prev is None or (e.get("edge") or 0) > (prev.get("edge") or 0):
            seen[key] = e
    return list(seen.values())


def leans_for_date(date_str=None, limit=50):
    """Load model leans for a date, including settlement outcomes if available."""
    target = str(date_str or _today_local_str())
    return _load_leans_for_date(target, limit=limit, include_outcomes=True)


def best_today(limit=15):
    return best_plays_for_date(_today_local_str(), limit=limit)


def results_for_date(date_str=None, limit=50):
    target = str(date_str or _yesterday_local_str())
    entries = _load_journal_entries()
    filtered = [e for e in entries if str(e.get("pickDate")) == target]
    deduped = _dedupe_latest(filtered)

    # Exclude user_supplied entries from validation metrics (hit rate, ROI, CLV)
    metric_entries = [e for e in deduped if not _is_excluded_book_entry(e)]
    summary = _summarize_settled(metric_entries)
    summary.update(_summarize_clv(metric_entries))
    unsettled = [e for e in deduped if not bool(e.get("settled"))]

    graded = [e for e in deduped if str(e.get("result")) in {"win", "loss", "push"}]
    graded.sort(key=lambda x: str(x.get("createdAtUtc", "")))
    limit_val = max(1, _as_int(limit, 50))

    rows = [
        {
            "entryId": e.get("entryId"),
            "createdAtLocal": e.get("createdAtLocal"),
            "playerId": e.get("playerId"),
            "playerName": e.get("playerName"),
            "stat": e.get("stat"),
            "line": e.get("line"),
            "actualStat": e.get("actualStat"),
            "side": e.get("recommendedSide"),
            "odds": e.get("recommendedOdds"),
            "lineAtBet": e.get("lineAtBet", e.get("line")),
            "oddsAtBet": e.get("oddsAtBet", e.get("recommendedOdds")),
            "closingLine": e.get("closingLine"),
            "closingOdds": e.get("closingOdds"),
            "clvLine": e.get("clvLine"),
            "clvOddsPct": e.get("clvOddsPct"),
            "result": e.get("result"),
            "pnl1u": e.get("pnl1u"),
            "recommendedEvPct": e.get("recommendedEvPct"),
        }
        for e in graded[:limit_val]
    ]

    return {
        "success": True,
        "date": target,
        "entriesLogged": len(filtered),
        "entriesUnique": len(deduped),
        "unsettledCount": len(unsettled),
        "summary": summary,
        "results": rows,
    }


def results_yesterday(limit=50):
    return results_for_date(_yesterday_local_str(), limit=limit)


def _american_to_implied_prob(odds):
    o = _as_float(odds)
    if o is None or o == 0:
        return None
    return 100.0 / (o + 100.0) if o > 0 else (-o) / ((-o) + 100.0)


def _select_export_format(output_path, fmt):
    if fmt:
        choice = str(fmt).lower().strip()
        if choice in {"csv", "jsonl"}:
            return choice
    suffix = Path(output_path).suffix.lower()
    return "jsonl" if suffix == ".jsonl" else "csv"


def export_training_rows(output_path, fmt=None, date_from=None, date_to=None):
    """
    Export settled journal entries into CSV/JSONL training rows.
    """
    path = Path(output_path)
    export_fmt = _select_export_format(path, fmt)

    entries = _dedupe_latest(_load_journal_entries())
    rows = []

    from_date = _parse_pick_date(date_from) if date_from else None
    to_date = _parse_pick_date(date_to) if date_to else None

    for e in entries:
        if not bool(e.get("settled")):
            continue
        actual = _as_float(e.get("actualStat"))
        if actual is None:
            continue

        try:
            pick_date = _parse_pick_date(e.get("pickDate"))
        except Exception:
            continue
        if from_date and pick_date < from_date:
            continue
        if to_date and pick_date > to_date:
            continue

        result = str(e.get("result") or "")
        if result == "win":
            hit = 1.0
        elif result == "loss":
            hit = 0.0
        elif result == "push":
            hit = 0.5
        else:
            hit = None

        line = _as_float(e.get("line"), 0.0)
        projection = _as_float(e.get("projection"), 0.0)
        over_odds = _as_float(e.get("overOdds"))
        under_odds = _as_float(e.get("underOdds"))
        implied_over = _american_to_implied_prob(over_odds)
        implied_under = _american_to_implied_prob(under_odds)

        row = {
            "pickDate": e.get("pickDate"),
            "playerId": _as_int(e.get("playerId"), 0),
            "playerName": e.get("playerName", ""),
            "stat": e.get("stat", ""),
            "opponentAbbr": e.get("opponentAbbr", ""),
            "isHome": 1 if bool(e.get("isHome")) else 0,
            "isB2B": 1 if bool(e.get("isB2B")) else 0,
            "line": line,
            "overOdds": over_odds,
            "underOdds": under_odds,
            "impliedOverProb": implied_over,
            "impliedUnderProb": implied_under,
            "projection": projection,
            "projStdev": _as_float(e.get("projStdev"), 0.0),
            "lineDiff": safe_round(projection - line, 4),
            "probOver": _as_float(e.get("probOver"), 0.0),
            "probUnder": _as_float(e.get("probUnder"), 0.0),
            "probPush": _as_float(e.get("probPush"), 0.0),
            "evOverPct": _as_float(e.get("evOverPct"), 0.0),
            "evUnderPct": _as_float(e.get("evUnderPct"), 0.0),
            "bestEvPct": _as_float(e.get("recommendedEvPct"), 0.0),
            "bestIsOver": 1 if str(e.get("recommendedSide")) == "over" else 0,
            "lineAtBet": _as_float(e.get("lineAtBet"), line),
            "oddsAtBet": _as_float(e.get("oddsAtBet")),
            "closingLine": _as_float(e.get("closingLine")),
            "closingOdds": _as_float(e.get("closingOdds")),
            "clvLine": _as_float(e.get("clvLine")),
            "clvOddsPct": _as_float(e.get("clvOddsPct")),
            "actual": actual,
            "hit": hit,
            "result": result,
            "pnl1u": _as_float(e.get("pnl1u"), 0.0),
        }
        rows.append(row)

    if not rows:
        return {
            "success": False,
            "error": "No settled rows available for export in selected date range.",
            "outputPath": str(path),
            "format": export_fmt,
            "rowCount": 0,
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    if export_fmt == "jsonl":
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
                f.write("\n")
    else:
        fieldnames = list(rows[0].keys())
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return {
        "success": True,
        "outputPath": str(path),
        "format": export_fmt,
        "rowCount": len(rows),
        "dateFrom": str(from_date) if from_date else None,
        "dateTo": str(to_date) if to_date else None,
        "sampleFields": list(rows[0].keys()),
    }


def _american_to_decimal(odds):
    o = _as_float(odds)
    if o is None or o == 0:
        return None
    if o > 0:
        return 1.0 + o / 100.0
    return 1.0 + 100.0 / abs(o)


def _clv_line_delta(side, line_at_bet, closing_line):
    side_key = str(side or "").lower()
    lb = _as_float(line_at_bet)
    lc = _as_float(closing_line)
    if lb is None or lc is None:
        return None
    if side_key == "over":
        return safe_round(lc - lb, 4)
    if side_key == "under":
        return safe_round(lb - lc, 4)
    return None


def _clv_odds_pct(odds_at_bet, closing_odds):
    bet_dec = _american_to_decimal(odds_at_bet)
    close_dec = _american_to_decimal(closing_odds)
    if bet_dec is None or close_dec is None or close_dec <= 0:
        return None
    return safe_round(((bet_dec / close_dec) - 1.0) * 100.0, 4)


def record_closing_values(date_str, updates):
    """
    Record closing lines/odds for existing entries and compute CLV metrics.

    updates: list of objects:
      - entryId (recommended), closingLine, closingOdds
      - or fallback selector: playerId + stat + line + recommendedSide
    """
    if not isinstance(updates, list):
        return {"success": False, "error": "updates must be a JSON array"}

    entries = _load_journal_entries()
    if not entries:
        return {"success": False, "error": "No journal entries found."}

    target = str(date_str or "")
    touched = 0
    missed = 0

    for upd in updates:
        if not isinstance(upd, dict):
            missed += 1
            continue

        entry = None
        entry_id = str(upd.get("entryId") or "").strip()
        if entry_id:
            entry = next(
                (e for e in entries if str(e.get("entryId")) == entry_id and (not target or str(e.get("pickDate")) == target)),
                None,
            )
        else:
            pid = _as_int(upd.get("playerId"))
            stat = str(upd.get("stat") or "").lower()
            line = _as_float(upd.get("line"))
            side = str(upd.get("recommendedSide") or "").lower()
            entry = next(
                (
                    e
                    for e in entries
                    if (not target or str(e.get("pickDate")) == target)
                    and (pid is None or _as_int(e.get("playerId")) == pid)
                    and (not stat or str(e.get("stat") or "").lower() == stat)
                    and (line is None or abs((_as_float(e.get("line"), 0.0) or 0.0) - line) < 1e-6)
                    and (not side or str(e.get("recommendedSide") or "").lower() == side)
                ),
                None,
            )

        if not entry:
            missed += 1
            continue

        closing_line = _as_float(upd.get("closingLine"))
        closing_odds = _as_int(upd.get("closingOdds"))
        if closing_line is None and closing_odds is None:
            missed += 1
            continue

        if closing_line is not None:
            entry["closingLine"] = closing_line
        if closing_odds is not None:
            entry["closingOdds"] = closing_odds

        entry["clvLine"] = _clv_line_delta(
            entry.get("recommendedSide"),
            entry.get("lineAtBet", entry.get("line")),
            entry.get("closingLine"),
        )
        entry["clvOddsPct"] = _clv_odds_pct(
            entry.get("oddsAtBet", entry.get("recommendedOdds")),
            entry.get("closingOdds"),
        )
        entry["clvComputedAtUtc"] = _now_utc_iso()
        touched += 1

    if touched > 0:
        _write_journal_entries(entries)

    return {
        "success": True,
        "date": target or None,
        "updatesRequested": len(updates),
        "updated": touched,
        "missed": missed,
    }


def backtest_lean_clv_report(source="backtest"):
    """
    Read enriched lean_bets_clv.jsonl and compute accuracy segmented by CLV.

    source: "backtest" reads lean_bets_clv.jsonl, "live" queries decision_journal.
    Returns overall, +CLV, -CLV accuracy with by-stat breakdown.
    """
    if source == "live":
        from .nba_decision_journal import DecisionJournal
        dj = DecisionJournal()
        try:
            return dj.lean_accuracy_clv()
        finally:
            dj.close()

    clv_path = DATA_DIR / "lean_bets_clv.jsonl"
    if not clv_path.exists():
        return {"success": False, "error": f"{clv_path} not found. Run scripts/enrich_leans_clv.py first."}

    leans = []
    with open(clv_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                leans.append(json.loads(line))

    def _get_result(r):
        return r.get("result") or r.get("outcome")

    settled = [l for l in leans if _get_result(l) in ("win", "loss", "push")]

    def _stats(recs, label):
        if not recs:
            return {"label": label, "sample": 0, "wins": 0, "hitRate": None, "pnl": 0, "roi": None}
        wins = sum(1 for r in recs if _get_result(r) == "win")
        non_push = sum(1 for r in recs if _get_result(r) in ("win", "loss"))
        pnl = sum(_as_float(r.get("pnl") or r.get("pnl_units"), 0.0) for r in recs)
        clv_vals = [r["clvDelta"] for r in recs if r.get("clvDelta") is not None]
        return {
            "label": label,
            "sample": len(recs),
            "wins": wins,
            "hitRate": safe_round(wins / non_push, 4) if non_push > 0 else None,
            "pnl": safe_round(pnl, 2),
            "roi": safe_round(pnl / len(recs), 4) if recs else None,
            "avgClvDelta": safe_round(sum(clv_vals) / len(clv_vals), 4) if clv_vals else None,
            "clvSample": len(clv_vals),
        }

    pos_clv = [l for l in settled if l.get("clvDelta") is not None and l["clvDelta"] > 0]
    neg_clv = [l for l in settled if l.get("clvDelta") is not None and l["clvDelta"] <= 0]
    no_clv = [l for l in settled if l.get("clvDelta") is None]

    # By stat
    by_stat: dict = {}
    for l in settled:
        s = str(l.get("stat") or "").lower()
        by_stat.setdefault(s, []).append(l)
    stat_out = {}
    for s, recs in by_stat.items():
        s_pos = [r for r in recs if r.get("clvDelta") is not None and r["clvDelta"] > 0]
        s_neg = [r for r in recs if r.get("clvDelta") is not None and r["clvDelta"] <= 0]
        stat_out[s] = {
            "all": _stats(recs, "all"),
            "posClv": _stats(s_pos, "+CLV"),
            "negClv": _stats(s_neg, "-CLV"),
        }

    has_clv = len(pos_clv) + len(neg_clv)
    return {
        "success": True,
        "source": source,
        "totalLeans": len(leans),
        "all": _stats(settled, "All Leans"),
        "posClv": _stats(pos_clv, "+CLV Confirmed"),
        "negClv": _stats(neg_clv, "-CLV Contrary"),
        "noClv": _stats(no_clv, "No CLV Data"),
        "clvCoverage": safe_round(has_clv / len(settled), 4) if settled else None,
        "byStat": stat_out,
    }


def enrich_journal_clv():
    """
    Backfill CLV on existing prop_journal.jsonl entries that have NULL closingLine.
    Uses OddsStore to look up closing lines.
    """
    from .nba_odds_store import OddsStore, STAT_TO_MARKET

    entries = _load_journal_entries()
    if not entries:
        return {"success": False, "error": "No journal entries found."}

    store = OddsStore()
    enriched = 0
    skipped = 0

    for entry in entries:
        if entry.get("closingLine") is not None:
            continue

        stat_key = str(entry.get("stat") or "").lower()
        market = STAT_TO_MARKET.get(stat_key)
        if not market:
            skipped += 1
            continue

        pick_date = str(entry.get("pickDate") or "")
        player_name = entry.get("playerName") or ""
        team_abbr = str(entry.get("teamAbbr") or entry.get("playerTeamAbbr") or "").upper()
        opp_abbr = str(entry.get("opponentAbbr") or "").upper()
        rec_side = str(entry.get("recommendedSide") or "").lower()

        if not pick_date or not player_name:
            skipped += 1
            continue

        event_id = store.find_event_for_game(team_abbr, opp_abbr, pick_date)
        cl = None
        if event_id:
            cl = store.get_closing_line(event_id, market, player_name)
        if not cl:
            cl = store.get_closing_line_by_player_date(player_name, market, pick_date)

        if not cl:
            skipped += 1
            continue

        close_line = cl.get("close_line")
        entry["closingLine"] = close_line
        entry["closingOdds"] = cl.get("close_over_odds") if rec_side == "over" else cl.get("close_under_odds")
        entry["clvLine"] = _clv_line_delta(
            rec_side,
            entry.get("lineAtBet", entry.get("line")),
            close_line,
        )
        rec_odds = entry.get("oddsAtBet", entry.get("recommendedOdds"))
        close_odds = entry.get("closingOdds")
        entry["clvOddsPct"] = _clv_odds_pct(rec_odds, close_odds)
        entry["clvComputedAtUtc"] = _now_utc_iso()
        enriched += 1

    store.close()

    if enriched > 0:
        _write_journal_entries(entries)

    return {
        "success": True,
        "totalEntries": len(entries),
        "enriched": enriched,
        "skipped": skipped,
        "alreadyHadClv": len(entries) - enriched - skipped,
    }
