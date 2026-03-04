#!/usr/bin/env python3
"""Bet journaling, settlement, daily reporting, and training export."""

import csv
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from nba_api.stats.endpoints import playergamelog
from nba_api.stats.static import players as nba_players_static

from .nba_data_collection import HEADERS, API_DELAY, retry_api_call, safe_round, safe_div, BETTING_POLICY
from .nba_toon import to_toon_table, toon_print_section

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
JOURNAL_PATH = DATA_DIR / "prop_journal.jsonl"

_PLAYERS_BY_ID = {
    int(p["id"]): str(p["full_name"])
    for p in nba_players_static.get_players()
    if p.get("id")
}


def _now_utc_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _today_local_str():
    return datetime.now().date().isoformat()


def _game_date_from_utc(commence_time_str):
    """Convert Odds API commenceTime (UTC ISO) to NBA game date using UTC-6 offset.
    NBA games tip off 7-10 PM ET; UTC-6 keeps the correct calendar date.
    Falls back to today if parsing fails."""
    try:
        s = str(commence_time_str or "").rstrip("Z").replace("T", " ")
        dt_utc = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return (dt_utc - timedelta(hours=6)).date().isoformat()
    except Exception:
        return _today_local_str()


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
    if not JOURNAL_PATH.exists():
        return []
    entries = []
    with JOURNAL_PATH.open("r", encoding="utf-8") as f:
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
    _ensure_data_dir()
    fd, tmp_path = tempfile.mkstemp(prefix="prop_journal_", suffix=".tmp", dir=str(DATA_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, separators=(",", ":"), ensure_ascii=False))
                f.write("\n")
        os.replace(tmp_path, JOURNAL_PATH)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _append_journal_entry(entry):
    _ensure_data_dir()
    with JOURNAL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))
        f.write("\n")


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
    # Derive pickDate from the game's commenceTime (UTC-6) so late-night games
    # (10 PM ET = UTC next day) aren't filed under the wrong calendar date.
    _commence = (prop_result or {}).get("commenceTime")
    _pick_date = _game_date_from_utc(_commence) if _commence else _today_local_str()
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

    try:
        _append_journal_entry(entry)
        return {"success": True, "entryId": entry["entryId"], "journalPath": str(JOURNAL_PATH)}
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
        filtered = [x for x in candidates if str(x[1]).upper() == str(opponent_abbr).upper()]
        if filtered:
            candidates = filtered

    if is_home is not None:
        filtered = [x for x in candidates if bool(x[2]) == bool(is_home)]
        if filtered:
            candidates = filtered

    return candidates[0][0] if candidates else None


def _extract_stat_from_row(row, stat):
    pts = _as_float(row.get("PTS"), 0.0)
    reb = _as_float(row.get("REB"), 0.0)
    ast = _as_float(row.get("AST"), 0.0)
    stl = _as_float(row.get("STL"), 0.0)
    blk = _as_float(row.get("BLK"), 0.0)
    tov = _as_float(row.get("TOV"), 0.0)
    fg3m = _as_float(row.get("FG3M"), 0.0)

    mapping = {
        "pts": pts,
        "reb": reb,
        "ast": ast,
        "stl": stl,
        "blk": blk,
        "tov": tov,
        "fg3m": fg3m,
        "pra": pts + reb + ast,
        "pr": pts + reb,
        "pa": pts + ast,
        "ra": reb + ast,
    }
    return mapping.get(str(stat or "").lower())


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
            "summary": _summarize_settled([e for e in entries if str(e.get("pickDate")) == str(date_str)]),
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

    _write_journal_entries(entries)

    same_day_entries = [e for e in entries if str(e.get("pickDate")) == str(date_str)]
    same_day_deduped = _dedupe_latest(same_day_entries)
    summary = _summarize_settled(same_day_deduped)
    summary.update(_summarize_clv(same_day_deduped))
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


def _get_playing_teams_today():
    """Return set of uppercase team abbreviations with a game today."""
    try:
        from .nba_data_collection import get_todays_games
        result = get_todays_games()
        teams = set()
        for g in result.get("games", []):
            h = g.get("homeTeam", {}).get("abbreviation", "")
            a = g.get("awayTeam", {}).get("abbreviation", "")
            if h:
                teams.add(h.upper())
            if a:
                teams.add(a.upper())
        return teams
    except Exception:
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
                    "settled": False,
                    "source": "sqlite_fallback",
                })
            return entries
    except Exception:
        return []


def best_plays_for_date(date_str=None, limit=15, unique_props=True):
    target = str(date_str or _today_local_str())
    entries = _load_journal_entries()
    filtered = [e for e in entries if str(e.get("pickDate")) == target]
    # Fallback: if JSONL has no entries for today, try SQLite (primary store)
    if not filtered:
        filtered = _sqlite_fallback_entries(target)
    deduped = _dedupe_latest(filtered)

    # Filter out phantom signals: players whose teams aren't playing today
    playing_teams = _get_playing_teams_today()
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
            "stat": e.get("stat"),
            "line": e.get("line"),
            "opponentAbbr": e.get("opponentAbbr"),
            "isHome": e.get("isHome"),
            "recommendedSide": e.get("recommendedSide"),
            "recommendedEvPct": e.get("recommendedEvPct"),
            "projection": e.get("projection"),
            "recommendedOdds": e.get("recommendedOdds"),
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
                open_line = _as_float(snaps[0].get("line"))
                curr_line = _as_float(snaps[-1].get("line"))
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
                        "snapshotCount": len(snaps),
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

    return {
        "success": True,
        "date": target,
        "totalRanked": len(ranked),
        "positiveEdgeCount": positive_edges,
        "policyQualified": policy_qualified,
        "topOffers": top_rows,
    }


def best_today(limit=15):
    return best_plays_for_date(_today_local_str(), limit=limit)


def results_for_date(date_str=None, limit=50):
    target = str(date_str or _yesterday_local_str())
    entries = _load_journal_entries()
    filtered = [e for e in entries if str(e.get("pickDate")) == target]
    deduped = _dedupe_latest(filtered)

    summary = _summarize_settled(deduped)
    summary.update(_summarize_clv(deduped))
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
