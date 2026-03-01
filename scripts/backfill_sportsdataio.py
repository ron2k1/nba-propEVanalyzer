#!/usr/bin/env python3
"""
Backfill SportsDataIO NBA feeds into local JSON files.

This script is designed for controlled daily quotas:
  - Resume support (skip files that already exist)
  - Request cap support
  - Optional line movement fetch per game
  - Manifest output for run auditing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from core.nba_sportsdataio import (
    SportsDataIOClient,
    extract_game_ids,
    iter_dates,
    to_sportsdataio_date,
)

load_dotenv(ROOT / ".env", override=True)


STATIC_FEEDS = (
    ("current_season", "fantasy/json/CurrentSeason"),
    ("players", "fantasy/json/Players"),
    ("free_agents", "fantasy/json/FreeAgents"),
    ("teams", "fantasy/json/Teams"),
    ("stadiums", "odds/json/Stadiums"),
)

REQUESTED_STATIC_FEEDS = (
    ("current_season", "fantasy/json/CurrentSeason"),
    ("players", "fantasy/json/Players"),
    ("free_agents", "fantasy/json/FreeAgents"),
    ("teams", "fantasy/json/Teams"),
    ("stadiums", "odds/json/Stadiums"),
)

SEASON_FEEDS = (
    ("standings", "fantasy/json/Standings/{season}"),
    ("schedules", "odds/json/Games/{season}"),
    ("team_season_stats", "odds/json/TeamSeasonStats/{season}"),
    ("player_season_stats", "fantasy/json/PlayerSeasonStats/{season}"),
    ("player_season_projections", "fantasy/json/PlayerSeasonProjectionStats/{season}"),
)

REQUESTED_SEASON_FEEDS = (
    ("standings", "fantasy/json/Standings/{season}"),
    ("schedules", "odds/json/Games/{season}"),
    ("team_season_stats", "odds/json/TeamSeasonStats/{season}"),
)

DATE_FEEDS = (
    ("games_by_date", "odds/json/GamesByDate/{date_token}"),
    ("game_odds_by_date", "odds/json/GameOddsByDate/{date_token}"),
    ("team_game_stats_by_date", "odds/json/TeamGameStatsByDate/{date_token}"),
    ("player_game_stats_by_date", "fantasy/json/PlayerGameStatsByDate/{date_token}"),
    (
        "player_game_projection_stats_by_date",
        "fantasy/json/PlayerGameProjectionStatsByDate/{date_token}",
    ),
    ("dfs_slates_by_date", "fantasy/json/DfsSlatesByDate/{date_token}"),
)

REQUESTED_DATE_FEEDS = (
    ("games_by_date", "odds/json/GamesByDate/{date_token}"),
    ("game_odds_by_date", "odds/json/GameOddsByDate/{date_token}"),
    ("team_game_stats_by_date", "odds/json/TeamGameStatsByDate/{date_token}"),
)

LINE_MOVEMENT_FEED = ("game_odds_line_movement", "odds/json/GameOddsLineMovement/{game_id}")


@dataclass
class FetchContext:
    client: SportsDataIOClient | None
    out_dir: Path
    request_count: int = 0
    saved_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    max_requests: int = 0
    sleep_sec: float = 0.0
    resume: bool = False
    dry_run: bool = False
    stopped_early: bool = False


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _can_fetch(ctx: FetchContext) -> bool:
    if ctx.max_requests <= 0:
        return True
    return ctx.request_count < ctx.max_requests


def _fetch_to_file(
    ctx: FetchContext,
    feed_name: str,
    endpoint: str,
    target_file: Path,
) -> tuple[bool, Any]:
    if ctx.resume and target_file.exists():
        ctx.skipped_count += 1
        return True, None

    if not _can_fetch(ctx):
        ctx.stopped_early = True
        return False, {"error": "max_requests reached"}

    if ctx.dry_run:
        ctx.request_count += 1
        ctx.saved_count += 1
        return True, {"dryRun": True, "feed": feed_name, "endpoint": endpoint}

    if ctx.client is None:
        ctx.failed_count += 1
        return False, {"feed": feed_name, "endpoint": endpoint, "error": "client is not initialized"}

    resp = ctx.client.get(endpoint)
    ctx.request_count += 1
    if not resp.get("success"):
        ctx.failed_count += 1
        return False, {
            "feed": feed_name,
            "endpoint": endpoint,
            "error": resp.get("error"),
            "statusCode": resp.get("statusCode"),
        }

    payload = {
        "fetchedAt": datetime.utcnow().isoformat() + "Z",
        "endpoint": endpoint,
        "url": resp.get("url"),
        "data": resp.get("data"),
    }
    _write_json(target_file, payload)
    ctx.saved_count += 1
    if ctx.sleep_sec > 0:
        time.sleep(ctx.sleep_sec)
    return True, payload


def _season_list(
    season_from: int | None,
    season_to: int | None,
    seasons_csv: str | None,
    current_season_hint: str | None,
) -> list[int]:
    if seasons_csv:
        out = []
        for tok in str(seasons_csv).split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                out.append(int(tok[:4]))
            except ValueError:
                continue
        return sorted(set(out))

    if season_from is not None and season_to is not None:
        lo, hi = min(season_from, season_to), max(season_from, season_to)
        return list(range(lo, hi + 1))

    if current_season_hint:
        try:
            return [int(str(current_season_hint)[:4])]
        except ValueError:
            pass

    return [datetime.utcnow().year]


def run_backfill(
    date_from: str,
    date_to: str,
    out_dir: str | Path = ROOT / "data" / "reference" / "sportsdataio" / "raw",
    season_from: int | None = None,
    season_to: int | None = None,
    seasons_csv: str | None = None,
    include_line_movement: bool = True,
    requested_only: bool = False,
    skip_empty_dates: bool = True,
    max_requests: int = 0,
    sleep_sec: float = 0.15,
    resume: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    out_path = Path(out_dir).resolve()
    try:
        client = None if dry_run else SportsDataIOClient()
    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "hint": "Set SPORTSDATA_API_KEY in .env or pass --dry-run.",
        }
    ctx = FetchContext(
        client=client,
        out_dir=out_path,
        max_requests=max_requests,
        sleep_sec=max(0.0, float(sleep_sec)),
        resume=resume,
        dry_run=dry_run,
    )
    errors: list[dict[str, Any]] = []

    static_feeds = REQUESTED_STATIC_FEEDS if requested_only else STATIC_FEEDS
    season_feeds = REQUESTED_SEASON_FEEDS if requested_only else SEASON_FEEDS
    date_feeds = REQUESTED_DATE_FEEDS if requested_only else DATE_FEEDS

    run_started = datetime.utcnow().isoformat() + "Z"
    print(
        f"[sportsdata_backfill] {date_from} -> {date_to}  "
        f"resume={resume} dry_run={dry_run} max_requests={max_requests}",
        flush=True,
    )

    # Static feeds first (also gives us current season for default season list).
    current_season_hint = None
    for feed_name, endpoint in static_feeds:
        target = out_path / "static" / f"{feed_name}.json"
        ok, payload = _fetch_to_file(ctx, feed_name, endpoint, target)
        if not ok:
            if ctx.stopped_early:
                break
            errors.append(payload or {"feed": feed_name, "endpoint": endpoint, "error": "unknown"})
            continue
        if feed_name == "current_season" and payload and not payload.get("dryRun"):
            current_season_hint = str((payload.get("data") or "")).strip()

    seasons = _season_list(season_from, season_to, seasons_csv, current_season_hint)

    # Season feeds.
    if not ctx.stopped_early:
        for season in seasons:
            for feed_name, endpoint_tmpl in season_feeds:
                endpoint = endpoint_tmpl.format(season=season)
                target = out_path / "season" / feed_name / f"{season}.json"
                ok, payload = _fetch_to_file(ctx, feed_name, endpoint, target)
                if not ok:
                    if ctx.stopped_early:
                        break
                    errors.append(
                        payload
                        or {
                            "feed": feed_name,
                            "season": season,
                            "endpoint": endpoint,
                            "error": "unknown",
                        }
                    )
            if ctx.stopped_early:
                break

    # Date feeds + per-game line movement.
    if not ctx.stopped_early:
        for date_str in iter_dates(date_from, date_to):
            date_token = to_sportsdataio_date(date_str)
            games_payload = None

            # Fetch games_by_date first so we can derive game IDs for line movement,
            # and optionally skip odds/stats pulls on empty slates.
            games_feed_name, games_endpoint_tmpl = date_feeds[0]
            games_endpoint = games_endpoint_tmpl.format(date_token=date_token)
            games_target = out_path / "date" / games_feed_name / f"{date_str}.json"
            ok, payload = _fetch_to_file(ctx, games_feed_name, games_endpoint, games_target)
            if not ok:
                if ctx.stopped_early:
                    break
                errors.append(
                    payload
                    or {
                        "feed": games_feed_name,
                        "date": date_str,
                        "endpoint": games_endpoint,
                        "error": "unknown",
                    }
                )
                continue

            if payload and not payload.get("dryRun"):
                games_payload = payload.get("data")
            elif games_target.exists():
                try:
                    games_payload = json.loads(games_target.read_text(encoding="utf-8")).get("data")
                except Exception:
                    games_payload = None

            game_ids = extract_game_ids(games_payload)
            if skip_empty_dates and not game_ids:
                continue

            for feed_name, endpoint_tmpl in date_feeds[1:]:
                endpoint = endpoint_tmpl.format(date_token=date_token)
                target = out_path / "date" / feed_name / f"{date_str}.json"
                ok, payload = _fetch_to_file(ctx, feed_name, endpoint, target)
                if not ok:
                    if ctx.stopped_early:
                        break
                    errors.append(
                        payload
                        or {
                            "feed": feed_name,
                            "date": date_str,
                            "endpoint": endpoint,
                            "error": "unknown",
                        }
                    )

            if ctx.stopped_early:
                break

            if include_line_movement:
                for game_id in game_ids:
                    feed_name, endpoint_tmpl = LINE_MOVEMENT_FEED
                    endpoint = endpoint_tmpl.format(game_id=game_id)
                    target = (
                        out_path
                        / "date"
                        / feed_name
                        / date_str
                        / f"{int(game_id)}.json"
                    )
                    ok, payload = _fetch_to_file(ctx, feed_name, endpoint, target)
                    if not ok:
                        if ctx.stopped_early:
                            break
                        errors.append(
                            payload
                            or {
                                "feed": feed_name,
                                "date": date_str,
                                "gameId": int(game_id),
                                "endpoint": endpoint,
                                "error": "unknown",
                            }
                        )
                if ctx.stopped_early:
                    break

    run_finished = datetime.utcnow().isoformat() + "Z"
    completed_all_requested = not ctx.stopped_early
    manifest = {
        "success": len(errors) == 0,
        "completedAllRequested": completed_all_requested,
        "runStarted": run_started,
        "runFinished": run_finished,
        "dateFrom": date_from,
        "dateTo": date_to,
        "seasons": seasons,
        "outDir": str(out_path),
        "includeLineMovement": include_line_movement,
        "requestedOnly": requested_only,
        "skipEmptyDates": skip_empty_dates,
        "requestCount": ctx.request_count,
        "savedCount": ctx.saved_count,
        "skippedCount": ctx.skipped_count,
        "failedCount": ctx.failed_count,
        "stoppedEarly": ctx.stopped_early,
        "maxRequests": max_requests,
        "dryRun": dry_run,
        "errors": errors[:200],
    }

    manifests_dir = out_path / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    manifest_path = manifests_dir / f"manifest_{stamp}.json"
    _write_json(manifest_path, manifest)
    manifest["manifestPath"] = str(manifest_path)
    return manifest


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill SportsDataIO NBA feeds to local JSON files.")
    p.add_argument("--date-from", required=True, help="Start date YYYY-MM-DD")
    p.add_argument("--date-to", required=True, help="End date YYYY-MM-DD")
    p.add_argument(
        "--out-dir",
        default=str(ROOT / "data" / "reference" / "sportsdataio" / "raw"),
        help="Output root directory",
    )
    p.add_argument("--season-from", type=int, default=None, help="Season start year (e.g., 2023)")
    p.add_argument("--season-to", type=int, default=None, help="Season end year (e.g., 2026)")
    p.add_argument("--seasons", default=None, help="Explicit seasons CSV, e.g. 2023,2024,2025,2026")
    p.add_argument(
        "--no-line-movement",
        action="store_true",
        help="Skip per-game line movement endpoint fetches",
    )
    p.add_argument(
        "--requested-only",
        action="store_true",
        help=(
            "Fetch only requested core feeds: CurrentSeason, Players, FreeAgents, Teams, "
            "Stadiums, Standings, Schedules, TeamSeasonStats, GamesByDate, "
            "GameOddsByDate, TeamGameStatsByDate, and GameOddsLineMovement."
        ),
    )
    p.add_argument(
        "--no-skip-empty-dates",
        action="store_true",
        help="Also fetch date-level odds/stats endpoints on dates with zero games.",
    )
    p.add_argument("--max-requests", type=int, default=0, help="Hard request cap. 0 = unlimited.")
    p.add_argument("--sleep-sec", type=float, default=0.15, help="Delay between API calls.")
    p.add_argument("--no-resume", action="store_true", help="Do not skip existing files.")
    p.add_argument("--dry-run", action="store_true", help="Count planned calls without writing.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    result = run_backfill(
        date_from=args.date_from,
        date_to=args.date_to,
        out_dir=args.out_dir,
        season_from=args.season_from,
        season_to=args.season_to,
        seasons_csv=args.seasons,
        include_line_movement=not args.no_line_movement,
        requested_only=args.requested_only,
        skip_empty_dates=not args.no_skip_empty_dates,
        max_requests=max(0, int(args.max_requests)),
        sleep_sec=max(0.0, float(args.sleep_sec)),
        resume=not args.no_resume,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
