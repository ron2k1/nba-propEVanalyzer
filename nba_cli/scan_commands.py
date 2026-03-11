#!/usr/bin/env python3
"""Scan commands: roster_sweep — scans LineStore snapshots and journals qualifying signals."""

from contextlib import contextmanager
import logging
import signal as _signal
import time as _time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from core.pipeline_progress import merge_status, read_status

_log = logging.getLogger("nba_engine.scan")
_LOCAL_TZ = ZoneInfo("America/New_York")
_PER_PLAYER_TIMEOUT_SEC = 45  # kill single compute_prop_ev if stuck


def _parse_roster_sweep_args(argv):
    date_str = date.today().isoformat()
    progress_file = None
    refresh_market_offers = False
    fetch_live_context = False
    use_local_projection_data = True

    idx = 2
    while idx < len(argv):
        token = str(argv[idx]).strip()
        if token in {"--verbose", "-v"}:
            idx += 1
            continue
        if token == "--progress-file" and idx + 1 < len(argv):
            progress_file = str(argv[idx + 1]).strip()
            idx += 2
            continue
        if token == "--refresh-market-offers":
            refresh_market_offers = True
            idx += 1
            continue
        if token == "--fetch-live-context":
            fetch_live_context = True
            idx += 1
            continue
        if token == "--live-projection-data":
            use_local_projection_data = False
            idx += 1
            continue
        if token == "--date" and idx + 1 < len(argv):
            date_str = str(argv[idx + 1]).strip() or date_str
            idx += 2
            continue
        if not token.startswith("--"):
            date_str = token or date_str
        idx += 1

    return {
        "date_str": date_str,
        "progress_file": progress_file,
        "refresh_market_offers": refresh_market_offers,
        "fetch_live_context": fetch_live_context,
        "use_local_projection_data": use_local_projection_data,
        "verbose": "--verbose" in argv or "-v" in argv,
    }


def _update_roster_sweep_progress(
    progress_file,
    *,
    date_str,
    stage,
    message,
    total=None,
    scanned=None,
    logged=None,
    leans_logged=None,
    current_player=None,
    current_stat=None,
    skip_reasons=None,
    completed=False,
    snapshot_only=True,
):
    existing = read_status(progress_file)
    task_name = existing.get("taskName") or "roster_sweep"
    merge_status(
        progress_file,
        taskName=task_name,
        currentCommand="roster_sweep",
        busy=not completed,
        stage=stage,
        date=date_str,
        message=message,
        snapshotOnly=bool(snapshot_only),
        total=total,
        scanned=scanned,
        logged=logged,
        leansLogged=leans_logged,
        currentPlayer=current_player,
        currentStat=current_stat,
        skipReasons=skip_reasons,
    )


def _snapshot_team_map_from_snaps(snaps):
    import re as _re

    mapping = {}
    for snap in snaps or []:
        pname = str(snap.get("player_name", "") or "").strip()
        team_abbr = str(snap.get("player_team_abbr", "") or "").upper().strip()
        if not pname or not team_abbr:
            continue
        norm_name = _re.sub(r"[.\-'']", "", pname).lower().strip()
        if norm_name:
            mapping[norm_name] = team_abbr
    return mapping


def _commence_local_date(commence_time):
    if not commence_time:
        return None
    try:
        dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(_LOCAL_TZ).date().isoformat()
    except Exception:
        return None


def _filter_snapshots_to_local_game_day(snaps, date_str):
    filtered = []
    for snap in snaps or []:
        local_day = _commence_local_date(snap.get("commence_time"))
        if local_day and local_day != date_str:
            continue
        filtered.append(snap)
    return filtered


@contextmanager
def _projection_data_context(use_local_projection_data):
    if not use_local_projection_data:
        yield {"mode": "live_api"}
        return

    try:
        from core.nba_local_stats import LocalNBAStats
        from core import nba_data_collection as _dc
        from core import nba_prep_projection as _pp

        provider = LocalNBAStats()

        _orig_gamelog = _dc.get_player_game_log
        _orig_splits = _dc.get_player_splits
        _orig_defense = _dc.get_team_defensive_ratings
        _orig_pos = _dc.get_player_position
        _orig_pvt = _dc.get_position_vs_team

        _orig_pp_gamelog = _pp.get_player_game_log
        _orig_pp_splits = _pp.get_player_splits
        _orig_pp_defense = _pp.get_team_defensive_ratings
        _orig_pp_pos = _pp.get_player_position
        _orig_pp_pvt = _pp.get_position_vs_team
        _orig_pp_api_delay = _pp.API_DELAY

        _dc.get_player_game_log = provider.get_player_game_log
        _dc.get_player_splits = provider.get_player_splits
        _dc.get_team_defensive_ratings = provider.get_team_defensive_ratings
        _dc.get_player_position = provider.get_player_position
        _dc.get_position_vs_team = provider.get_position_vs_team

        _pp.get_player_game_log = provider.get_player_game_log
        _pp.get_player_splits = provider.get_player_splits
        _pp.get_team_defensive_ratings = provider.get_team_defensive_ratings
        _pp.get_player_position = provider.get_player_position
        _pp.get_position_vs_team = provider.get_position_vs_team
        _pp.API_DELAY = 0.0

        try:
            yield {
                "mode": "local_index",
                "provider": provider,
            }
        finally:
            _dc.get_player_game_log = _orig_gamelog
            _dc.get_player_splits = _orig_splits
            _dc.get_team_defensive_ratings = _orig_defense
            _dc.get_player_position = _orig_pos
            _dc.get_position_vs_team = _orig_pvt

            _pp.get_player_game_log = _orig_pp_gamelog
            _pp.get_player_splits = _orig_pp_splits
            _pp.get_team_defensive_ratings = _orig_pp_defense
            _pp.get_player_position = _orig_pp_pos
            _pp.get_position_vs_team = _orig_pp_pvt
            _pp.API_DELAY = _orig_pp_api_delay
    except Exception as exc:
        _log.warning("LocalNBAStats unavailable (%s), falling back to live API — roster_sweep will be slow", exc)
        yield {"mode": "live_api"}


def _handle_roster_sweep(argv):
    """
    roster_sweep [date]

    For each unique (player, stat) in today's LineStore snapshots:
      1. Calls compute_prop_ev() with the current book line
      2. If _qualifies(): logs to DecisionJournal
    Returns summary: {scanned, logged, skipped, top5}
    """
    from core.nba_line_store import LineStore
    from core.nba_decision_journal import DecisionJournal, _qualifies
    from core.gates import SIGNAL_SPEC, CURRENT_SIGNAL_VERSION
    from core.nba_model_training import american_to_implied_prob, compute_prop_ev
    from core.nba_data_collection import safe_round, get_yesterdays_team_abbrs, get_todays_game_totals, get_player_team_map
    from core.nba_bet_tracking import log_prop_ev_entry
    from nba_api.stats.static import players as nba_players_static

    parsed = _parse_roster_sweep_args(argv)
    date_str = parsed["date_str"]
    progress_file = parsed["progress_file"]
    refresh_market_offers = parsed["refresh_market_offers"]
    fetch_live_context = parsed["fetch_live_context"]
    use_local_projection_data = parsed["use_local_projection_data"]
    snapshot_only = not refresh_market_offers

    # Default path is snapshot-first and local-data-first; live context is opt-in.
    _update_roster_sweep_progress(
        progress_file,
        date_str=date_str,
        stage="loading_context",
        message="Loading stored snapshots and evaluation context.",
        snapshot_only=snapshot_only,
    )
    yesterday_teams = set()
    game_totals = {}
    if fetch_live_context:
        yesterday_teams = get_yesterdays_team_abbrs(date_str)
        game_totals = get_todays_game_totals(date_str)

    ls = LineStore()
    snaps = _filter_snapshots_to_local_game_day(ls.get_snapshots(date_str), date_str)
    if not snaps:
        _update_roster_sweep_progress(
            progress_file,
            date_str=date_str,
            stage="completed",
            message="No snapshots found for date.",
            total=0,
            scanned=0,
            logged=0,
            leans_logged=0,
            completed=True,
            snapshot_only=snapshot_only,
        )
        return {
            "success": True,
            "date": date_str,
            "scanned": 0,
            "logged": 0,
            "skipped": 0,
            "message": "No snapshots found for date",
        }

    # Deduplicate: keep latest snapshot per (player, stat, book)
    latest = {}
    for s in sorted(snaps, key=lambda x: x.get("timestamp_utc", "")):
        key = (s.get("player_name", ""), s.get("stat", ""), s.get("book", ""))
        latest[key] = s

    # Further deduplicate: per (player, stat), pick the highest-priority book.
    # Pinnacle is tracked separately as a reference (not a betting book) so it
    # can populate referenceBook for the Pinnacle confirmation gate in _qualifies().
    # Priority order: betmgm > draftkings > fanduel > other
    BOOK_PRIO = {"betmgm": 0, "draftkings": 1, "fanduel": 2}
    best_per_player_stat = {}
    pinnacle_per_player_stat = {}
    for (pname, stat, book), snap in latest.items():
        key2 = (pname, stat)
        if book.lower() == "pinnacle":
            pinnacle_per_player_stat[key2] = snap
            continue
        existing = best_per_player_stat.get(key2)
        if existing is None:
            best_per_player_stat[key2] = snap
        else:
            if BOOK_PRIO.get(book, 99) < BOOK_PRIO.get(existing.get("book", ""), 99):
                best_per_player_stat[key2] = snap

    # ---- Pulled-lines detection: skip players whose props were removed ----
    # Group raw snapshots by collect_lines batch (10-min window).
    # If a player appeared in an earlier FULL sweep but NOT in the latest
    # full sweep, books likely pulled their lines (player ruled OUT).
    _MIN_FULL_SWEEP = 50  # batch must have ≥50 unique players to count
    _batch_players: dict[str, set] = {}
    for snap in snaps:
        if (snap.get("book") or "").lower() == "pinnacle":
            continue
        ts = snap.get("timestamp_utc", "")[:16]
        _batch_players.setdefault(ts, set()).add(snap.get("player_name", ""))
    # Keep only full-sweep batches
    full_batches = {k: v for k, v in _batch_players.items() if len(v) >= _MIN_FULL_SWEEP}
    pulled_lines_players: set = set()
    if len(full_batches) >= 2:
        sorted_batches = sorted(full_batches.keys())
        latest_batch_key = sorted_batches[-1]
        latest_batch_names = full_batches[latest_batch_key]
        # Players in ANY earlier full batch but absent from the latest
        earlier_names: set = set()
        for bk in sorted_batches[:-1]:
            earlier_names |= full_batches[bk]
        pulled_lines_players = earlier_names - latest_batch_names
        if pulled_lines_players:
            _log.info("Pulled-lines filter: %d players absent from latest batch (%s): %s",
                      len(pulled_lines_players), latest_batch_key,
                      ", ".join(sorted(pulled_lines_players)[:10]))
            for pname in pulled_lines_players:
                keys_to_remove = [k for k in best_per_player_stat if k[0] == pname]
                for k in keys_to_remove:
                    del best_per_player_stat[k]

    import re as _re

    def _norm_name(n):
        """Normalize player name for matching."""
        return _re.sub(r"[.\-'']", "", str(n)).lower().strip()

    _player_team_map = _snapshot_team_map_from_snaps(best_per_player_stat.values())
    if fetch_live_context:
        try:
            live_player_team_map = get_player_team_map()
            for norm_name, team_abbr in (live_player_team_map or {}).items():
                _player_team_map.setdefault(norm_name, team_abbr)
        except Exception:
            pass

    # Accept --verbose flag for debug-level trace logging
    _verbose = parsed["verbose"]
    if _verbose:
        logging.getLogger("nba_engine").setLevel(logging.DEBUG)

    scanned = 0
    logged = 0
    lean_count = 0
    skipped_list = []
    top_results = []
    total_candidates = len(best_per_player_stat)

    _update_roster_sweep_progress(
        progress_file,
        date_str=date_str,
        stage="evaluating",
        message=f"Evaluating {total_candidates} player/stat snapshots.",
        total=total_candidates,
        scanned=0,
        logged=0,
        leans_logged=0,
        snapshot_only=snapshot_only,
    )

    dj = DecisionJournal()
    projection_data_scope = _projection_data_context(use_local_projection_data)
    projection_state = projection_data_scope.__enter__()
    projection_data_source = projection_state.get("mode", "live_api")
    _sweep_start = _time.monotonic()
    _SWEEP_MAX_SEC = 900  # 15-min hard ceiling; abort gracefully if exceeded

    if projection_data_source == "live_api" and use_local_projection_data:
        _log.warning("Local projection data requested but unavailable — sweep will use live API (slow)")

    for (pname, stat), snap in best_per_player_stat.items():
        # Abort if total sweep time exceeds ceiling
        if _time.monotonic() - _sweep_start > _SWEEP_MAX_SEC:
            _log.warning("roster_sweep hit %ds ceiling after %d/%d players — aborting remaining", _SWEEP_MAX_SEC, scanned, total_candidates)
            skipped_list.append({"player": "(remaining)", "stat": "*", "reason": "sweep_timeout"})
            break
        scanned += 1
        if scanned == 1 or scanned % 10 == 0 or scanned == total_candidates:
            _update_roster_sweep_progress(
                progress_file,
                date_str=date_str,
                stage="evaluating",
                message=f"Evaluating {scanned}/{total_candidates}: {pname} {stat}.",
                total=total_candidates,
                scanned=scanned,
                logged=logged,
                leans_logged=lean_count,
                current_player=pname,
                current_stat=stat,
                snapshot_only=snapshot_only,
            )
        try:
            # Resolve player ID via exact then partial match
            matches = nba_players_static.find_players_by_full_name(pname)
            if not matches:
                matches = [
                    p for p in nba_players_static.get_players()
                    if pname.lower() in p.get("full_name", "").lower()
                ]
            if not matches:
                skipped_list.append({"player": pname, "stat": stat, "reason": "player_not_found"})
                continue
            player_id = matches[0]["id"]
            player_name = matches[0]["full_name"]

            line = snap.get("line")
            over_odds = snap.get("over_odds", -110)
            under_odds = snap.get("under_odds", -110)
            book = snap.get("book", "")
            team_abbr = snap.get("player_team_abbr", "")
            opp_abbr = snap.get("opponent_abbr", "")
            is_home = snap.get("is_home")

            # Enrich missing team/opponent from player→team map + event context
            if not team_abbr:
                team_abbr = _player_team_map.get(_norm_name(pname), "")
            h = snap.get("home_team_abbr", "").upper()
            a = snap.get("away_team_abbr", "").upper()
            if team_abbr and not opp_abbr:
                if team_abbr.upper() == h:
                    opp_abbr = a
                    is_home = True
                elif team_abbr.upper() == a:
                    opp_abbr = h
                    is_home = False

            # Skip phantom players: team not in this event's matchup
            if team_abbr and h and a and team_abbr.upper() not in (h, a):
                skipped_list.append({"player": pname, "stat": stat, "reason": "team_not_in_event"})
                continue

            if line is None or not opp_abbr:
                skipped_list.append({"player": pname, "stat": stat, "reason": "missing_line_or_opponent"})
                continue

            opp_is_b2b  = opp_abbr.upper() in yesterday_teams
            gtotal      = game_totals.get(frozenset({
                (team_abbr or "").upper(), opp_abbr.upper()
            }))

            _log.debug("Evaluating %s/%s: line=%.1f book=%s opp=%s", pname, stat, float(line), book, opp_abbr)
            from datetime import datetime as _dt_cls, timezone as _tz_cls
            _swept_at = _dt_cls.now(_tz_cls.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _t0 = _time.monotonic()
            result = compute_prop_ev(
                player_id=player_id,
                opponent_abbr=opp_abbr,
                is_home=bool(is_home) if is_home is not None else True,
                stat=stat,
                line=float(line),
                over_odds=int(over_odds or -110),
                under_odds=int(under_odds or -110),
                is_b2b=False,
                player_team_abbr=team_abbr or None,
                refresh_market_offers=refresh_market_offers,
                as_of_date=date_str,
                opponent_is_b2b=opp_is_b2b,
                game_total=gtotal,
            )
            _elapsed = _time.monotonic() - _t0
            if _elapsed > _PER_PLAYER_TIMEOUT_SEC:
                _log.warning("compute_prop_ev for %s/%s took %.1fs (> %ds) — possible API fallback", pname, stat, _elapsed, _PER_PLAYER_TIMEOUT_SEC)

            if not result.get("success"):
                skipped_list.append({
                    "player": pname, "stat": stat,
                    "reason": result.get("error", "ev_failed"),
                })
                continue

            # Inject Pinnacle referenceBook from LineStore snapshots if available.
            # No extra API call needed — Pinnacle data was already fetched by collect_lines.
            # This activates the Pinnacle confirmation gate in _qualifies().
            pinn_snap = pinnacle_per_player_stat.get((pname, stat))
            if pinn_snap and result.get("referenceBook") is None:
                _po = american_to_implied_prob(pinn_snap.get("over_odds"))
                _pu = american_to_implied_prob(pinn_snap.get("under_odds"))
                if _po and _pu and (_po + _pu) > 0:
                    _t = _po + _pu
                    result["referenceBook"] = {
                        "book": "pinnacle",
                        "line": pinn_snap.get("line"),
                        "overOdds": pinn_snap.get("over_odds"),
                        "underOdds": pinn_snap.get("under_odds"),
                        "noVigOver": safe_round(_po / _t, 4),
                        "noVigUnder": safe_round(_pu / _t, 4),
                    }

            qualifies_ok, skip_reason = _qualifies(result, stat, used_real_line=True)
            _ev = result.get("ev") or {}
            _eo_dbg = float((_ev.get("over") or {}).get("edge") or 0.0)
            _eu_dbg = float((_ev.get("under") or {}).get("edge") or 0.0)
            _log.debug("  %s/%s: edge=%.4f conf=%.4f probOver=%.4f qualifies=%s reason=%s",
                        pname, stat, max(_eo_dbg, _eu_dbg),
                        max(float(_ev.get("probOver") or 0.0), float(_ev.get("probUnder") or 0.0)),
                        float(_ev.get("probOver") or 0.0), qualifies_ok, skip_reason or "")
            if not qualifies_ok:
                skipped_list.append({"player": pname, "stat": stat, "reason": skip_reason})
                # Log as lean if: eligible stat + positive edge
                _eligible = SIGNAL_SPEC[CURRENT_SIGNAL_VERSION]["eligible_stats"]
                _max_edge = max(_eo_dbg, _eu_dbg)
                if stat in _eligible and _max_edge > 0:
                    _lean_side = "over" if _eo_dbg >= _eu_dbg else "under"
                    _lean_proj = (result.get("projection") or {}).get("projection", 0.0)
                    _lean_conf = max(
                        float(_ev.get("probOver") or 0.0),
                        float(_ev.get("probUnder") or 0.0),
                    )
                    dj.log_lean(
                        player_id=player_id, player_name=player_name,
                        team_abbr=team_abbr or "", opponent_abbr=opp_abbr,
                        stat=stat, line=float(line), book=book,
                        over_odds=int(over_odds or -110), under_odds=int(under_odds or -110),
                        projection=float(_lean_proj),
                        prob_over=float(_ev.get("probOver") or 0.0),
                        prob_under=float(_ev.get("probUnder") or 0.0),
                        edge_over=_eo_dbg, edge_under=_eu_dbg,
                        recommended_side=_lean_side, recommended_edge=_max_edge,
                        confidence=_lean_conf,
                        skip_reason=skip_reason,
                        context={
                            "source": "roster_sweep",
                            "book": book,
                            "projectionDataSource": projection_data_source,
                            "fetchLiveContext": fetch_live_context,
                        },
                        swept_at=_swept_at,
                    )
                    lean_count += 1
                continue

            ev = result.get("ev") or {}
            eo = float((ev.get("over") or {}).get("edge") or 0.0)
            eu = float((ev.get("under") or {}).get("edge") or 0.0)
            rec = "over" if eo >= eu else "under"
            proj = result.get("projection") or {}

            ctx = {
                "source": "roster_sweep",
                "book": book,
                "snapshotTs": snap.get("timestamp_utc"),
                "oppIsB2B": opp_is_b2b,
                "projectionDataSource": projection_data_source,
                "fetchLiveContext": fetch_live_context,
            }
            if gtotal is not None:
                ctx["gameTotal"] = gtotal
            ref_book = result.get("referenceBook")
            if ref_book:
                ctx["referenceBook"] = ref_book
            hv = (result.get("projection") or {}).get("recentHighVariance")
            if hv is not None:
                ctx["recentHighVariance"] = hv

            djr = dj.log_signal(
                player_id=player_id, player_name=player_name,
                team_abbr=team_abbr or "", opponent_abbr=opp_abbr,
                stat=stat, line=float(line), book=book,
                over_odds=int(over_odds or -110), under_odds=int(under_odds or -110),
                projection=float(proj.get("projection") or 0.0),
                prob_over=float(ev.get("probOver") or 0.0),
                prob_under=float(ev.get("probUnder") or 0.0),
                edge_over=eo, edge_under=eu, recommended_side=rec,
                recommended_edge=max(eo, eu),
                confidence=max(
                    float(ev.get("probOver") or 0.0),
                    float(ev.get("probUnder") or 0.0),
                ),
                used_real_line=True, action_taken=0,
                context=ctx,
                swept_at=_swept_at,
            )
            if djr.get("isDuplicate"):
                skipped_list.append({"player": pname, "stat": stat, "reason": "duplicate"})
                try:
                    bridge_result = dict(result)
                    if not bridge_result.get("commenceTime"):
                        bridge_result["commenceTime"] = snap.get("commence_time")
                    log_prop_ev_entry(
                        bridge_result,
                        player_id=player_id,
                        player_identifier=player_name,
                        player_team_abbr=team_abbr or "",
                        opponent_abbr=opp_abbr,
                        is_home=bool(is_home) if is_home is not None else True,
                        stat=stat,
                        line=float(line),
                        over_odds=int(over_odds or -110),
                        under_odds=int(under_odds or -110),
                        is_b2b=False,
                        source="roster_sweep",
                        swept_at=_swept_at,
                    )
                except Exception as _bridge_ex:
                    _log.warning("JSONL bridge write failed for %s/%s duplicate: %s", pname, stat, _bridge_ex)
            elif djr.get("success"):
                logged += 1
                top_results.append({
                    "player": player_name,
                    "stat": stat,
                    "line": line,
                    "side": rec,
                    "edge": safe_round(max(eo, eu), 4),
                    "probOver": safe_round(float(ev.get("probOver") or 0.0), 4),
                    "book": book,
                })
                # Bridge: also write to prop_journal.jsonl so best_today can see it
                try:
                    bridge_result = dict(result)
                    if not bridge_result.get("commenceTime"):
                        bridge_result["commenceTime"] = snap.get("commence_time")
                    log_prop_ev_entry(
                        bridge_result,
                        player_id=player_id,
                        player_identifier=player_name,
                        player_team_abbr=team_abbr or "",
                        opponent_abbr=opp_abbr,
                        is_home=bool(is_home) if is_home is not None else True,
                        stat=stat,
                        line=float(line),
                        over_odds=int(over_odds or -110),
                        under_odds=int(under_odds or -110),
                        is_b2b=False,
                        source="roster_sweep",
                        swept_at=_swept_at,
                    )
                except Exception as _bridge_ex:
                    _log.warning("JSONL bridge write failed for %s/%s: %s", pname, stat, _bridge_ex)
            else:
                skipped_list.append({
                    "player": pname, "stat": stat,
                    "reason": djr.get("error", "log_failed"),
                })

        except Exception as ex:
            _log.warning("roster_sweep error for %s/%s: %s", pname, stat, ex)
            skipped_list.append({"player": pname, "stat": stat, "reason": str(ex)})

    projection_data_scope.__exit__(None, None, None)
    dj.close()

    top5 = sorted(top_results, key=lambda x: -x["edge"])[:5]

    # Summarize skip reasons for diagnostics
    skip_reasons = {}
    for s in skipped_list:
        r = s.get("reason", "unknown")
        skip_reasons[r] = skip_reasons.get(r, 0) + 1

    _update_roster_sweep_progress(
        progress_file,
        date_str=date_str,
        stage="completed",
        message=f"Roster sweep finished. Logged {logged} signals and {lean_count} leans.",
        total=total_candidates,
        scanned=scanned,
        logged=logged,
        leans_logged=lean_count,
        skip_reasons=skip_reasons,
        completed=True,
        snapshot_only=snapshot_only,
    )

    result = {
        "success": True,
        "date": date_str,
        "scanned": scanned,
        "logged": logged,
        "leansLogged": lean_count,
        "skipped": len(skipped_list),
        "skipReasons": skip_reasons,
        "snapshotOnly": snapshot_only,
        "fetchLiveContext": fetch_live_context,
        "projectionDataSource": projection_data_source,
        "top5": top5,
    }
    if pulled_lines_players:
        result["pulledLinesFiltered"] = sorted(pulled_lines_players)
    return result


def _handle_top_picks(argv):
    """
    top_picks [limit]

    Show top N policy-qualified picks for today + best 2-leg parlay.
    Default limit is 5.
    """
    from itertools import combinations
    from core.nba_bet_tracking import best_plays_for_date
    from core.nba_parlay_engine import compute_parlay_ev
    from core.nba_data_collection import safe_round

    limit = int(argv[2]) if len(argv) > 2 else 5

    result = best_plays_for_date(limit=max(limit, 20))
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "best_plays_for_date failed")}

    all_offers = result.get("topOffers") or []
    qualified = [r for r in all_offers if r.get("policyQualified")]

    # Load full journal entries for enrichment (probOver, odds, book not in topOffers)
    from core.nba_bet_tracking import _load_journal_entries, _today_local_str
    target = result.get("date") or _today_local_str()
    entries = _load_journal_entries()
    journal_by_key = {}
    for e in entries:
        if str(e.get("pickDate")) != target:
            continue
        key = (e.get("playerId"), str(e.get("stat", "")).lower(), e.get("line"))
        journal_by_key[key] = e  # latest wins (entries are time-sorted)

    top = qualified[:limit]

    top_picks = []
    for i, r in enumerate(top, 1):
        ev_pct = r.get("recommendedEvPct") or 0.0
        pid = r.get("playerId")
        stat = str(r.get("stat", "")).lower()
        je = journal_by_key.get((pid, stat, r.get("line"))) or {}
        _pick = {
            "rank": i,
            "playerName": r.get("playerName"),
            "stat": r.get("stat"),
            "line": r.get("line"),
            "side": r.get("recommendedSide"),
            "evPct": safe_round(float(ev_pct), 2),
            "projection": r.get("projection"),
            "odds": r.get("recommendedOdds"),
            "probOver": je.get("probOver"),
            "book": je.get("bestOverBook") or je.get("bestUnderBook") or "",
            "opponentAbbr": r.get("opponentAbbr"),
        }
        # Thread sweep timing from best_plays_for_date output
        if r.get("sweptAtUtc"):
            _pick["sweptAtUtc"] = r["sweptAtUtc"]
        elif r.get("sweptAtFallback"):
            _pick["sweptAtFallback"] = r["sweptAtFallback"]
        top_picks.append(_pick)

    # --- Best 2-leg parlay from top picks ---
    best_parlay = None
    if len(qualified) >= 2:
        parlay_candidates = qualified[:min(len(qualified), 8)]  # cap combos
        best_ev = -999
        for a, b in combinations(parlay_candidates, 2):
            # Skip same-player parlays (correlated, most books reject)
            if a.get("playerId") == b.get("playerId"):
                continue
            legs = []
            for pick in (a, b):
                pid = pick.get("playerId")
                stat = str(pick.get("stat", "")).lower()
                je = journal_by_key.get((pid, stat, pick.get("line"))) or {}
                side = str(pick.get("recommendedSide", "over")).lower()
                legs.append({
                    "probOver": je.get("probOver", 0.5),
                    "side": side,
                    "overOdds": je.get("overOdds", -110),
                    "underOdds": je.get("underOdds", -110),
                    "playerId": pid or 0,
                    "playerTeam": je.get("playerTeamAbbr", ""),
                    "stat": stat,
                    "line": pick.get("line", 0),
                })
            pr = compute_parlay_ev(legs)
            if pr.get("success") and (pr.get("evPercent", -999) > best_ev):
                best_ev = pr["evPercent"]
                best_parlay = {
                    "leg1": {
                        "playerName": a.get("playerName"),
                        "stat": a.get("stat"),
                        "line": a.get("line"),
                        "side": a.get("recommendedSide"),
                    },
                    "leg2": {
                        "playerName": b.get("playerName"),
                        "stat": b.get("stat"),
                        "line": b.get("line"),
                        "side": b.get("recommendedSide"),
                    },
                    "jointProb": pr.get("jointProb"),
                    "parlayOdds": pr.get("parlayAmericanOdds"),
                    "evPercent": pr.get("evPercent"),
                    "correlationImpact": pr.get("correlationImpact"),
                    "verdict": pr.get("verdict"),
                }

    out = {
        "success": True,
        "date": result.get("date"),
        "topPicks": top_picks,
        "bestParlay": best_parlay,
        "message": f"Top {len(top_picks)} picks" + (" + best 2-leg parlay" if best_parlay else ""),
    }

    # Human-readable summary
    print(f"\n=== TOP PICKS  {out['date']} ===")
    for p in top_picks:
        print(f"  #{p['rank']}  {p['playerName']}  {p['stat']} {p['side']} {p['line']}  EV={p['evPct']:.1f}%  proj={p['projection']}  odds={p['odds']}")
    if best_parlay:
        l1, l2 = best_parlay["leg1"], best_parlay["leg2"]
        print(f"\n  PARLAY: {l1['playerName']} {l1['stat']} {l1['side']} {l1['line']}")
        print(f"       + {l2['playerName']} {l2['stat']} {l2['side']} {l2['line']}")
        print(f"       EV={best_parlay['evPercent']:.1f}%  odds={best_parlay['parlayOdds']}  verdict={best_parlay['verdict']}")
    print()

    return out


_COMMANDS = {
    "roster_sweep": _handle_roster_sweep,
    "top_picks": _handle_top_picks,
}
