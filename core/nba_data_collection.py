#!/usr/bin/env python3
"""Data collection layer for NBA pipeline."""

# Agent navigation index (high-level):
# - Core helpers/config: lines ~1-260
# - Odds API + player prop offer extraction: lines ~288-855
# - NBA stats pulls (games/players/defense): lines ~856-1667
# - Live-game stat extraction: lines ~1668+
# For Tier 2 changes, most projection behavior is in `nba_prep_projection.py` and
# `nba_prop_engine.py`; this file mainly handles fetch/normalize/caching.

import json
import logging
import time
import math
import os
import hashlib
import re
import unicodedata
import traceback
import statistics
from datetime import datetime, timedelta, timezone

import requests

_log = logging.getLogger("nba_engine.data")

from nba_api.stats.endpoints import (
    scoreboardv3,
    playergamelog,
    leaguedashteamstats,
    leaguedashplayerstats,
    playerdashboardbygeneralsplits,
    commonplayerinfo,
    commonteamroster,
)
from nba_api.stats.static import teams as nba_teams_static, players as nba_players_static

def get_season_string():
    now = datetime.now()
    y   = now.year
    return f"{y}-{str(y + 1)[-2:]}" if now.month >= 10 else f"{y - 1}-{str(y)[-2:]}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer":    "https://www.nba.com/",
    "Accept":     "application/json",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token":  "true",
}
API_DELAY = 0.7  # seconds between NBA Stats API calls
_GAMELOG_CACHE_TTL = int(os.getenv("GAMELOG_CACHE_TTL", "3600"))
_SPLITS_CACHE_TTL  = int(os.getenv("SPLITS_CACHE_TTL",  "3600"))
_DEFENSE_CACHE_TTL = int(os.getenv("DEFENSE_CACHE_TTL", "3600"))
_PVT_CACHE_TTL     = int(os.getenv("PVT_CACHE_TTL",     "3600"))
PROJECTION_CONFIG = {
    "defense_adj":   (0.70, 1.40),   # defense multiplier min/max
    "home_away":     (0.85, 1.15),   # home/away split cap
    "rest_b2b":      (0.80, 1.05),   # B2B penalty cap
    "rest_rested":   (0.95, 1.10),   # rested bonus cap
    "matchup":       (0.70, 1.40),   # matchup history factor cap
    "mins_trend":    (0.85, 1.15),   # minutes trend cap
    "combined":      (0.55, 1.60),   # total adjustment compound cap
    "dnp_min_threshold": 1,          # exclude games with min < this (DNP filter)
    "min_edge_threshold": 0.08,      # raised 2026-03-01: 0.05→0.08 (30-40%/60-80% bins losing on 87d real-line data)
}

BETTING_POLICY = {
    "stat_whitelist": {"pts", "ast"},  # reb removed 2026-02-28: -5.34% ROI; pra removed 2026-03-01: -3.81% ROI on 318 real-line bets
    "blocked_prob_bins": {1, 2, 3, 4, 5, 6, 7, 8},  # bins 1+8 added 2026-03-03: bin 1 +4.3% ROI/28.9 cal error; bin 8 n=11 insufficient. Active: 0 (0-10%) + 9 (90-100%)
    "min_ev_pct": 0.0,                 # evPercent floor
}
CURRENT_SEASON = get_season_string()
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".nba_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)
_ALL_TEAMS_BY_ABBR = {t["abbreviation"]: t for t in nba_teams_static.get_teams()}
_ALL_TEAMS_BY_ID   = {t["id"]:           t for t in nba_teams_static.get_teams()}
_ACTIVE_PLAYERS = nba_players_static.get_active_players()
_ACTIVE_PLAYERS_BY_ID = {int(p["id"]): p for p in _ACTIVE_PLAYERS}
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_DEFAULT_SPORT = "basketball_nba"
ODDS_DEFAULT_REGIONS = "us"
ODDS_DEFAULT_MARKETS = "h2h,spreads,totals"
ODDS_PLAYER_PROP_MARKET_BY_STAT = {
    "pts": "player_points",
    "reb": "player_rebounds",
    "ast": "player_assists",
    "fg3m": "player_threes",
    "stl": "player_steals",
    "blk": "player_blocks",
    "tov": "player_turnovers",
    "pra": "player_points_rebounds_assists",
    "pr": "player_points_rebounds",
    "pa": "player_points_assists",
    "ra": "player_rebounds_assists",
}

def _local_game_log_fallback(player_id, season, last_n=25, as_of_date=None):
    try:
        from .nba_local_stats import LocalNBAStats
        provider = LocalNBAStats(index_path=(os.getenv("NBA_LOCAL_INDEX_PATH") or None))
        out = provider.get_player_game_log(
            player_id=player_id,
            season=season,
            last_n=last_n,
            as_of_date=as_of_date,
        )
        if out.get("success") and out.get("gameLogs"):
            out["source"] = "local_index_fallback"
            out["fallbackReason"] = "nba_api_unavailable"
            return out, None
        return None, "local_index_no_games"
    except Exception as e:
        return None, f"local_index_error:{type(e).__name__}"


def _cache_path(key):
    return os.path.join(_CACHE_DIR, hashlib.md5(key.encode()).hexdigest() + ".json")

def cache_get(key, ttl):
    """Return cached data if within ttl seconds, else None."""
    try:
        p = _cache_path(key)
        if os.path.exists(p):
            with open(p) as f:
                entry = json.load(f)
            if time.time() - entry["ts"] < ttl:
                return entry["data"]
    except Exception:
        _log.debug("cache_get failed for key=%s: %s", key[:40], traceback.format_exc(limit=1).strip())
    return None

def cache_set(key, data):
    try:
        with open(_cache_path(key), "w") as f:
            json.dump({"ts": time.time(), "data": data}, f)
    except Exception:
        _log.debug("cache_set failed for key=%s: %s", key[:40], traceback.format_exc(limit=1).strip())

def team_id_from_abbr(abbr):
    t = _ALL_TEAMS_BY_ABBR.get(abbr)
    return t["id"] if t else None

def _normalize_player_name(name):
    raw = str(name or "").strip().lower()
    # Remove diacritics so "Jokić" matches "Jokic".
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", raw)).strip()

def _format_player_candidate(player):
    return {
        "id": int(player.get("id", 0) or 0),
        "name": str(player.get("full_name", "") or ""),
        "firstName": str(player.get("first_name", "") or ""),
        "lastName": str(player.get("last_name", "") or ""),
    }

def search_players_by_name(query, limit=20):
    """
    Lightweight local lookup over active players (no network call).
    """
    q = _normalize_player_name(query)
    if not q:
        return {
            "success": False,
            "error": "name_query required",
            "query": str(query or ""),
            "matches": [],
        }

    exact = []
    starts = []
    contains = []
    for p in _ACTIVE_PLAYERS:
        full_name = str(p.get("full_name", "") or "")
        norm = _normalize_player_name(full_name)
        if not norm:
            continue
        candidate = _format_player_candidate(p)
        if norm == q:
            exact.append(candidate)
            continue
        if norm.startswith(q):
            starts.append(candidate)
        elif q in norm:
            contains.append(candidate)

    def _sort_key(item):
        # Keep deterministic order and favor shorter names for prefixes.
        return (len(item["name"]), item["name"])

    if exact:
        return {
            "success": True,
            "query": str(query),
            "matches": sorted(exact, key=_sort_key)[:max(1, int(limit or 20))],
            "exact": True,
        }

    merged = sorted(starts, key=_sort_key) + sorted(contains, key=_sort_key)
    return {
        "success": True,
        "query": str(query),
        "matches": merged[:max(1, int(limit or 20))],
        "exact": False,
    }

def resolve_player_identifier(player_identifier):
    """
    Resolve either numeric player ID or player name into a single active player ID.
    Accepts values like:
      - 203507
      - "Anthony Edwards"
      - "Anthony Edwards (1630162)"
    """
    raw = str(player_identifier or "").strip()
    if not raw:
        return {"success": False, "error": "player identifier is required"}

    # Support "Name (12345)" convenience input.
    id_match = re.search(r"\((\d+)\)\s*$", raw)
    if id_match:
        raw = id_match.group(1)

    try:
        player_id = int(raw)
        p = _ACTIVE_PLAYERS_BY_ID.get(player_id)
        if p:
            return {
                "success": True,
                "playerId": player_id,
                "playerName": str(p.get("full_name", "")),
                "matchType": "id",
            }
        return {
            "success": False,
            "error": f"Active player ID not found: {player_id}",
            "candidates": [],
        }
    except (TypeError, ValueError):
        pass

    search = search_players_by_name(raw, limit=12)
    if not search.get("success"):
        return search

    matches = search.get("matches", [])
    if len(matches) == 1:
        m = matches[0]
        return {
            "success": True,
            "playerId": int(m["id"]),
            "playerName": m["name"],
            "matchType": "name",
        }
    if not matches:
        return {
            "success": False,
            "error": f"No active player matched '{raw}'",
            "candidates": [],
        }
    return {
        "success": False,
        "error": f"Ambiguous player name '{raw}'.",
        "ambiguous": True,
        "candidates": matches,
    }

def safe_round(val, decimals=1):
    if val is None:
        return 0.0
    try:
        return round(float(val), decimals)
    except (TypeError, ValueError):
        return 0.0

def safe_div(a, b, default=0.0):
    if b is None or b == 0:
        return default
    return a / b

def retry_api_call(func, max_retries=3, delay=1.5):
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                raise e

def _parse_iso_datetime(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None

def _coerce_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        # datetime.date compatible
        try:
            return datetime(value.year, value.month, value.day).date()
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def _game_date_obj(raw):
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def _get_odds_api_key():
    key = os.getenv("ODDS_API_KEY", "").strip()
    # Be tolerant of accidental wrapped quotes from shell/startup scripts.
    if len(key) >= 2 and ((key[0] == key[-1] == '"') or (key[0] == key[-1] == "'")):
        key = key[1:-1].strip()
    return key

def _odds_api_get(path, params=None, timeout=30):
    api_key = _get_odds_api_key()
    if not api_key:
        return {
            "success": False,
            "error": "ODDS_API_KEY not set. Add your Odds API key from the-odds-api.com.",
        }

    query = dict(params or {})
    query["apiKey"] = api_key

    url = f"{ODDS_API_BASE_URL}/{path.lstrip('/')}"
    try:
        resp = requests.get(url, params=query, timeout=timeout)
        if resp.status_code != 200:
            text = (resp.text or "").strip()
            return {
                "success": False,
                "error": f"Odds API HTTP {resp.status_code}",
                "details": text[:500],
            }
        return {
            "success": True,
            "data": resp.json(),
            "quota": {
                "remaining": resp.headers.get("x-requests-remaining"),
                "used": resp.headers.get("x-requests-used"),
                "last": resp.headers.get("x-requests-last"),
            },
        }
    except Exception as e:
        msg = str(e)
        if "WinError 10013" in msg:
            msg = (
                f"{msg} "
                "Windows blocked the outbound socket. Allow python.exe in Windows Firewall "
                "or run the UI launcher elevated (run_ui.ps1)."
            )
        return {"success": False, "error": msg}

def _odds_price_to_decimal(price, odds_format):
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None

    if odds_format == "decimal":
        return p if p > 1.0 else None

    # American odds
    if p == 0:
        return None
    if p > 0:
        return 1.0 + (p / 100.0)
    return 1.0 + (100.0 / abs(p))

def _contains_player_prop_markets(markets_csv):
    markets = [m.strip().lower() for m in str(markets_csv or "").split(",") if m.strip()]
    return any(m.startswith("player_") for m in markets)

def _normalize_text(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower())).strip()

def _tokens_no_suffix(value):
    suffixes = {"jr", "sr", "ii", "iii", "iv", "v"}
    toks = [t for t in _normalize_text(value).split(" ") if t and t not in suffixes]
    return toks

def _player_name_matches(target_name, candidate_name):
    t_toks = _tokens_no_suffix(target_name)
    c_toks = _tokens_no_suffix(candidate_name)
    if not t_toks or not c_toks:
        return False

    if t_toks == c_toks:
        return True

    # Common robust fallback: match first + last name pair.
    if len(t_toks) >= 2 and len(c_toks) >= 2:
        if t_toks[0] == c_toks[0] and t_toks[-1] == c_toks[-1]:
            return True

    t_join = " ".join(t_toks)
    c_join = " ".join(c_toks)
    if len(t_toks) >= 2 and t_join in c_join:
        return True
    if len(c_toks) >= 2 and c_join in t_join:
        return True
    return False

def _team_aliases_for_abbr(team_abbr):
    ab = str(team_abbr or "").upper().strip()
    t = _ALL_TEAMS_BY_ABBR.get(ab)
    if not t:
        return [_normalize_text(ab)] if ab else []

    aliases = {
        _normalize_text(ab),
        _normalize_text(t.get("full_name", "")),
        _normalize_text(t.get("city", "")),
        _normalize_text(t.get("nickname", "")),
        _normalize_text(f"{t.get('city', '')} {t.get('nickname', '')}"),
    }
    return [x for x in aliases if x]

def _team_name_matches_abbr(event_team_name, team_abbr):
    event_norm = _normalize_text(event_team_name)
    if not event_norm:
        return False
    aliases = _team_aliases_for_abbr(team_abbr)
    for alias in aliases:
        if alias == event_norm:
            return True
    for alias in aliases:
        if len(alias) >= 4 and (alias in event_norm or event_norm in alias):
            return True
    return False

def _extract_player_offer_side_and_name(outcome):
    """
    Supports both common formats:
      A) name=Over, description=<Player>
      B) name=<Player>, description=Over
    """
    name_raw = str((outcome or {}).get("name", "") or "")
    desc_raw = str((outcome or {}).get("description", "") or "")
    participant_raw = str((outcome or {}).get("participant", "") or "")

    name_norm = _normalize_text(name_raw)
    desc_norm = _normalize_text(desc_raw)

    if name_norm in {"over", "under"}:
        return name_norm, (desc_raw or participant_raw)
    if desc_norm in {"over", "under"}:
        return desc_norm, (name_raw or participant_raw)

    # Fallback: some feeds use "Over 24.5" style in name.
    if name_norm.startswith("over"):
        return "over", (desc_raw or participant_raw)
    if name_norm.startswith("under"):
        return "under", (desc_raw or participant_raw)
    if desc_norm.startswith("over"):
        return "over", (name_raw or participant_raw)
    if desc_norm.startswith("under"):
        return "under", (name_raw or participant_raw)
    return None, None


def _normalize_bookmaker_key(book_name):
    return re.sub(r"[^a-z0-9]+", "", str(book_name or "").lower())


def _bookmaker_priority(book_name):
    key = _normalize_bookmaker_key(book_name)
    if "betmgm" in key:
        return 3
    if "draftkings" in key:
        return 2
    if "fanduel" in key:
        return 1
    return 0

def _extract_odds_discrepancies(events, odds_format):
    """
    Build a discrepancy list for the same outcome across bookmakers.
    Larger gap means larger potential shopping edge.
    """
    quotes_by_outcome = {}
    event_meta = {}

    for event in events or []:
        eid = event.get("id", "")
        event_meta[eid] = {
            "eventId": eid,
            "homeTeam": event.get("home_team", ""),
            "awayTeam": event.get("away_team", ""),
            "commenceTime": event.get("commence_time", ""),
        }
        for bm in event.get("bookmakers", []) or []:
            bname = bm.get("title") or bm.get("key") or "unknown"
            for market in bm.get("markets", []) or []:
                mkey = market.get("key", "")
                for out in market.get("outcomes", []) or []:
                    oname = out.get("name", "")
                    point = out.get("point")
                    price = out.get("price")
                    dec = _odds_price_to_decimal(price, odds_format)
                    if dec is None:
                        continue
                    key = (eid, mkey, oname, point)
                    quotes_by_outcome.setdefault(key, []).append({
                        "bookmaker": bname,
                        "price": price,
                        "decimal": dec,
                    })

    discrepancies = []
    for key, quotes in quotes_by_outcome.items():
        if len(quotes) < 2:
            continue
        best = max(quotes, key=lambda x: x["decimal"])
        worst = min(quotes, key=lambda x: x["decimal"])
        if not best or not worst:
            continue
        eid, mkey, oname, point = key
        gap_pct = safe_round(((best["decimal"] / worst["decimal"]) - 1.0) * 100.0, 2) if worst["decimal"] > 0 else 0.0
        meta = event_meta.get(eid, {})
        discrepancies.append({
            **meta,
            "market": mkey,
            "outcome": oname,
            "point": point,
            "bookCount": len(quotes),
            "bestPrice": best["price"],
            "bestBookmaker": best["bookmaker"],
            "worstPrice": worst["price"],
            "worstBookmaker": worst["bookmaker"],
            "valueGapPct": gap_pct,
        })

    discrepancies.sort(key=lambda x: x.get("valueGapPct", 0), reverse=True)
    return discrepancies

def get_nba_sportsbook_odds(
    regions=ODDS_DEFAULT_REGIONS,
    markets=ODDS_DEFAULT_MARKETS,
    bookmakers=None,
    sport=ODDS_DEFAULT_SPORT,
    odds_format="american",
):
    """
    Pregame odds across books. Returns raw events and discrepancy summary.
    """
    if _contains_player_prop_markets(markets):
        return {
            "success": False,
            "error": (
                "Player prop markets are not supported by this sportsbook endpoint. "
                "Use auto_sweep (event-level player prop fetch) for props."
            ),
            "requestedMarkets": markets,
        }

    cache_key = f"odds_pre_{sport}_{regions}_{markets}_{bookmakers}_{odds_format}"
    cached = cache_get(cache_key, 30)
    if cached:
        return cached

    params = {
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }
    if bookmakers:
        params["bookmakers"] = bookmakers

    fetched = _odds_api_get(f"sports/{sport}/odds", params=params, timeout=30)
    if not fetched.get("success"):
        return {"success": False, "error": fetched.get("error"), "details": fetched.get("details")}

    now = datetime.utcnow()
    events = []
    for e in fetched.get("data", []) or []:
        commence = _parse_iso_datetime(e.get("commence_time"))
        if commence and commence.replace(tzinfo=None) <= now:
            continue
        events.append(e)

    out = {
        "success": True,
        "sport": sport,
        "mode": "pregame",
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "events": events,
        "eventCount": len(events),
        "discrepancies": _extract_odds_discrepancies(events, odds_format),
        "quota": fetched.get("quota"),
    }
    cache_set(cache_key, out)
    return out

def get_nba_live_odds(
    regions=ODDS_DEFAULT_REGIONS,
    markets=ODDS_DEFAULT_MARKETS,
    bookmakers=None,
    sport=ODDS_DEFAULT_SPORT,
    odds_format="american",
    max_events=8,
):
    """
    Live/in-play odds on demand using live event detection + event odds fetch.
    """
    if _contains_player_prop_markets(markets):
        return {
            "success": False,
            "error": (
                "Player prop markets are not supported by this live sportsbook endpoint. "
                "Use auto_sweep for player props."
            ),
            "requestedMarkets": markets,
        }

    cache_key = f"odds_live_{sport}_{regions}_{markets}_{bookmakers}_{odds_format}_{max_events}"
    cached = cache_get(cache_key, 20)
    if cached:
        return cached

    score_params = {"daysFrom": 1, "dateFormat": "iso"}
    score_data = _odds_api_get(f"sports/{sport}/scores", params=score_params, timeout=25)
    if not score_data.get("success"):
        return {"success": False, "error": score_data.get("error"), "details": score_data.get("details")}

    now = datetime.utcnow()
    live_event_ids = []
    for e in score_data.get("data", []) or []:
        if e.get("completed") is True:
            continue
        commence = _parse_iso_datetime(e.get("commence_time"))
        if commence and commence.replace(tzinfo=None) <= now:
            eid = e.get("id")
            if eid:
                live_event_ids.append(eid)

    live_event_ids = live_event_ids[:max_events]
    if not live_event_ids:
        return {
            "success": True,
            "sport": sport,
            "mode": "live",
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
            "events": [],
            "eventCount": 0,
            "discrepancies": [],
            "quota": score_data.get("quota"),
            "note": "No in-play events detected right now.",
        }

    params = {
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
        "eventIds": ",".join(live_event_ids),
    }
    if bookmakers:
        params["bookmakers"] = bookmakers

    odds_data = _odds_api_get(f"sports/{sport}/odds", params=params, timeout=30)
    if not odds_data.get("success"):
        return {"success": False, "error": odds_data.get("error"), "details": odds_data.get("details")}

    events = odds_data.get("data", []) or []
    out = {
        "success": True,
        "sport": sport,
        "mode": "live",
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "events": events,
        "eventCount": len(events),
        "liveEventIds": live_event_ids,
        "discrepancies": _extract_odds_discrepancies(events, odds_format),
        "quota": odds_data.get("quota"),
    }
    cache_set(cache_key, out)
    return out

def get_nba_player_prop_offers(
    player_name,
    player_team_abbr,
    opponent_abbr,
    is_home,
    stat,
    regions=ODDS_DEFAULT_REGIONS,
    bookmakers=None,
    sport=ODDS_DEFAULT_SPORT,
    odds_format="american",
):
    """
    Fetch available player prop offers (line + over/under odds) for one player/stat
    across sportsbooks for the upcoming selected matchup.
    """
    stat_key = str(stat or "").lower().strip()
    market_key = ODDS_PLAYER_PROP_MARKET_BY_STAT.get(stat_key)
    if not market_key:
        return {
            "success": False,
            "error": f"Unsupported stat '{stat_key}' for prop market sweep.",
            "supportedStats": sorted(ODDS_PLAYER_PROP_MARKET_BY_STAT.keys()),
        }

    player_name_clean = str(player_name or "").strip()
    if not player_name_clean:
        return {"success": False, "error": "player_name required for prop offer lookup."}

    team_abbr = str(player_team_abbr or "").upper().strip()
    opp_abbr = str(opponent_abbr or "").upper().strip()
    if not team_abbr or not opp_abbr:
        return {"success": False, "error": "player_team_abbr and opponent_abbr are required."}

    cache_key = (
        f"odds_prop_{sport}_{regions}_{bookmakers}_{team_abbr}_{opp_abbr}_"
        f"{int(bool(is_home))}_{stat_key}_{_normalize_text(player_name_clean)}"
    )
    cached = cache_get(cache_key, 20)
    if cached:
        return cached

    # 1) Find event ID for the specified matchup.
    event_params = {
        "regions": regions,
        "markets": "h2h",
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }
    if bookmakers:
        event_params["bookmakers"] = bookmakers

    events_resp = _odds_api_get(f"sports/{sport}/odds", params=event_params, timeout=30)
    if not events_resp.get("success"):
        return {"success": False, "error": events_resp.get("error"), "details": events_resp.get("details")}

    events = events_resp.get("data", []) or []
    now = datetime.utcnow()
    future_events = []
    for event in events:
        commence = _parse_iso_datetime(event.get("commence_time"))
        if commence:
            age = (now - commence.replace(tzinfo=None)).total_seconds()
            if age > 4 * 3600:  # exclude games that started more than 4 hours ago
                continue
        future_events.append(event)

    exp_home = team_abbr if bool(is_home) else opp_abbr
    exp_away = opp_abbr if bool(is_home) else team_abbr

    strict = []
    relaxed = []
    for event in future_events:
        home_name = event.get("home_team", "")
        away_name = event.get("away_team", "")
        home_ok = _team_name_matches_abbr(home_name, exp_home)
        away_ok = _team_name_matches_abbr(away_name, exp_away)
        if home_ok and away_ok:
            strict.append(event)
            continue

        has_player_team = (
            _team_name_matches_abbr(home_name, team_abbr)
            or _team_name_matches_abbr(away_name, team_abbr)
        )
        has_opp_team = (
            _team_name_matches_abbr(home_name, opp_abbr)
            or _team_name_matches_abbr(away_name, opp_abbr)
        )
        if has_player_team and has_opp_team:
            relaxed.append(event)

    candidates = strict or relaxed
    if not candidates:
        return {
            "success": False,
            "error": (
                f"No upcoming event found for {team_abbr} vs {opp_abbr}. "
                "Try checking team abbreviations or wait for market availability."
            ),
            "sport": sport,
            "regions": regions,
        }

    def _event_sort_key(event):
        commence = _parse_iso_datetime(event.get("commence_time"))
        if not commence:
            return datetime.max
        return commence.replace(tzinfo=None)

    selected_event = sorted(candidates, key=_event_sort_key)[0]
    event_id = selected_event.get("id")
    if not event_id:
        return {"success": False, "error": "Matched event missing event ID from odds API."}

    # 2) Pull event-specific player prop market.
    prop_params = {
        "regions": regions,
        "markets": market_key,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }
    if bookmakers:
        prop_params["bookmakers"] = bookmakers

    prop_resp = _odds_api_get(
        f"sports/{sport}/events/{event_id}/odds",
        params=prop_params,
        timeout=30,
    )
    if not prop_resp.get("success"):
        return {"success": False, "error": prop_resp.get("error"), "details": prop_resp.get("details")}

    payload = prop_resp.get("data", {}) or {}
    books = payload.get("bookmakers", []) or []

    # 3) Parse offers into [bookmaker, line, overOdds, underOdds]
    offers = []
    for bm in books:
        book_name = bm.get("title") or bm.get("key") or "unknown"
        line_map = {}
        for market in bm.get("markets", []) or []:
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []) or []:
                side, outcome_player = _extract_player_offer_side_and_name(outcome)
                if side not in {"over", "under"}:
                    continue
                if not _player_name_matches(player_name_clean, outcome_player):
                    continue
                point = outcome.get("point")
                if point is None:
                    continue
                try:
                    line_val = float(point)
                except (TypeError, ValueError):
                    continue
                odds_val = outcome.get("price")
                if odds_val is None:
                    continue

                record = line_map.setdefault(
                    line_val,
                    {
                        "bookmaker": book_name,
                        "line": safe_round(line_val, 3),
                        "overOdds": None,
                        "underOdds": None,
                    },
                )
                if side == "over":
                    record["overOdds"] = odds_val
                else:
                    record["underOdds"] = odds_val

        for _, offer in sorted(line_map.items(), key=lambda x: x[0]):
            if offer["overOdds"] is None or offer["underOdds"] is None:
                continue
            offers.append(offer)

    deduped = {}
    for offer in offers:
        key = (offer.get("bookmaker"), safe_round(offer.get("line"), 3))
        deduped[key] = offer

    offers_out = list(deduped.values())
    offers_out.sort(
        key=lambda x: (
            -_bookmaker_priority(x.get("bookmaker")),
            float(x.get("line", 0)),
            str(x.get("bookmaker", "")),
        )
    )

    out = {
        "success": True,
        "sport": sport,
        "regions": regions,
        "marketKey": market_key,
        "stat": stat_key,
        "playerName": player_name_clean,
        "playerTeamAbbr": team_abbr,
        "opponentAbbr": opp_abbr,
        "isHome": bool(is_home),
        "eventId": event_id,
        "eventHomeTeam": selected_event.get("home_team"),
        "eventAwayTeam": selected_event.get("away_team"),
        "commenceTime": selected_event.get("commence_time"),
        "bookmakersRequested": bookmakers,
        "offerCount": len(offers_out),
        "offers": offers_out,
        "quota": prop_resp.get("quota"),
        "discoveryQuota": events_resp.get("quota"),
    }
    cache_set(cache_key, out)
    return out


def get_event_player_props_bulk(
    event_id,
    stat,
    bookmakers=None,
    sport=ODDS_DEFAULT_SPORT,
    odds_format="american",
):
    """
    Fetch all player prop lines for ALL players in a single event for one stat.
    Unlike get_nba_player_prop_offers(), returns every player found (no name filter).

    Used by the line-history collector to snapshot all props for a game in one call.

    Returns:
      {
        "success": bool,
        "eventId": str,
        "stat": str,
        "marketKey": str,
        "snapshots": [
          {"player_name": str, "book": str, "line": float,
           "over_odds": int, "under_odds": int, "game_id": str}
        ],
        "quota": {...}
      }
    """
    stat_key   = str(stat or "").lower().strip()
    market_key = ODDS_PLAYER_PROP_MARKET_BY_STAT.get(stat_key)
    if not market_key:
        return {"success": False, "error": f"unsupported stat '{stat_key}'", "snapshots": []}

    prop_params = {
        "regions":    ODDS_DEFAULT_REGIONS,
        "markets":    market_key,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }
    if bookmakers:
        prop_params["bookmakers"] = bookmakers

    resp = _odds_api_get(f"sports/{sport}/events/{event_id}/odds", params=prop_params, timeout=30)
    if not resp.get("success"):
        return {"success": False, "error": resp.get("error"), "snapshots": [],
                "quota": resp.get("quota")}

    payload  = resp.get("data", {}) or {}
    books    = payload.get("bookmakers", []) or []

    snapshots = []
    for bm in books:
        book_name = bm.get("title") or bm.get("key") or "unknown"
        # Collect per-player, per-line records within this bookmaker
        player_lines = {}   # (player_name, line_val) → record
        for market in bm.get("markets", []) or []:
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []) or []:
                side, player_name = _extract_player_offer_side_and_name(outcome)
                if side not in {"over", "under"} or not player_name:
                    continue
                point = outcome.get("point")
                if point is None:
                    continue
                try:
                    line_val = float(point)
                except (TypeError, ValueError):
                    continue
                price = outcome.get("price")
                if price is None:
                    continue

                rec = player_lines.setdefault(
                    (player_name, line_val),
                    {
                        "player_name": player_name,
                        "book":        book_name,
                        "game_id":     event_id,
                        "stat":        stat_key,
                        "line":        safe_round(line_val, 1),
                        "over_odds":   None,
                        "under_odds":  None,
                    },
                )
                if side == "over":
                    rec["over_odds"] = price
                else:
                    rec["under_odds"] = price

        for rec in player_lines.values():
            if rec["over_odds"] is not None and rec["under_odds"] is not None:
                snapshots.append(rec)

    return {
        "success":   True,
        "eventId":   event_id,
        "stat":      stat_key,
        "marketKey": market_key,
        "snapshots": snapshots,
        "quota":     resp.get("quota"),
    }


def get_todays_event_props_bulk(
    bookmakers="betmgm,draftkings,fanduel",
    stats=None,
    sport=ODDS_DEFAULT_SPORT,
    odds_format="american",
):
    """
    Snapshot ALL player prop lines for today's games across specified books and stats.

    Returns a flat list of snapshot dicts ready for LineStore.append_snapshots(), each
    enriched with timestamp_utc, player_team_abbr (best-effort), opponent_abbr,
    is_home, and commence_time.

    stats: list of stat keys (default: ["pts","reb","ast","fg3m","tov"])
           pass ["all"] to fetch all 11 markets (uses more API quota)

    API cost: 1 quota request per (event × market).  Default 5 stats × N_games.
    """
    if stats is None:
        stats = ["pts", "reb", "ast", "fg3m", "tov"]
    elif stats == ["all"]:
        stats = list(ODDS_PLAYER_PROP_MARKET_BY_STAT.keys())

    # 1) Discover today's events via the h2h endpoint (single call)
    events_resp = _odds_api_get(
        f"sports/{sport}/odds",
        params={
            "regions":    ODDS_DEFAULT_REGIONS,
            "markets":    "h2h",
            "oddsFormat": odds_format,
            "dateFormat": "iso",
        },
        timeout=30,
    )
    if not events_resp.get("success"):
        return {
            "success": False,
            "error":   events_resp.get("error"),
            "snapshots": [],
            "quota":   events_resp.get("quota"),
        }

    # Filter events to today's NBA schedule date (US/Eastern) + allow
    # live games that started within the last 4 hours.
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("US/Eastern")
    now      = datetime.now(timezone.utc)
    today_et = now.astimezone(_ET).date()

    def _event_is_today(e):
        ct = _parse_iso_datetime(e.get("commence_time"))
        if not ct:
            return False                       # no commence_time → skip
        ct_et = ct.astimezone(_ET)
        if ct_et.date() != today_et:
            return False                       # wrong date → skip
        age = (now - ct).total_seconds()
        if age > 4 * 3600:
            return False                       # tipped off > 4 h ago → skip
        return True

    events = [e for e in (events_resp.get("data") or []) if _event_is_today(e)]

    if not events:
        return {
            "success":   True,
            "snapshots": [],
            "eventCount": 0,
            "message":   "no active events found",
            "quota":     events_resp.get("quota"),
        }

    timestamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_snapshots = []
    errors        = []
    _ptm          = get_player_team_map()

    for event in events:
        event_id      = event.get("id", "")
        home_name     = event.get("home_team", "")
        away_name     = event.get("away_team", "")
        commence_time = event.get("commence_time", "")

        # Best-effort team abbr from name
        home_abbr = _abbr_from_team_name(home_name)
        away_abbr = _abbr_from_team_name(away_name)

        for stat in stats:
            result = get_event_player_props_bulk(
                event_id=event_id,
                stat=stat,
                bookmakers=bookmakers,
                sport=sport,
                odds_format=odds_format,
            )
            if not result.get("success"):
                errors.append({"event_id": event_id, "stat": stat,
                               "error": result.get("error")})
                continue

            for snap in result.get("snapshots", []):
                _pname_norm = re.sub(r"[.\-\u2019']", "", snap["player_name"]).lower().strip()
                _resolved_team = _ptm.get(_pname_norm, "")
                _opp = away_abbr if _resolved_team == home_abbr else (
                    home_abbr if _resolved_team == away_abbr else ""
                )
                _is_home = True if _resolved_team == home_abbr else (
                    False if _resolved_team == away_abbr else None
                )
                all_snapshots.append({
                    "timestamp_utc":    timestamp_utc,
                    "game_id":          event_id,
                    "player_name":      snap["player_name"],
                    "player_team_abbr": _resolved_team,
                    "opponent_abbr":    _opp,
                    "is_home":          _is_home,
                    "stat":             stat,
                    "line":             snap["line"],
                    "over_odds":        snap["over_odds"],
                    "under_odds":       snap["under_odds"],
                    "book":             snap["book"],
                    "commence_time":    commence_time,
                    "home_team_abbr":   home_abbr,
                    "away_team_abbr":   away_abbr,
                })

    return {
        "success":     True,
        "timestamp":   timestamp_utc,
        "eventCount":  len(events),
        "statCount":   len(stats),
        "snapshots":   all_snapshots,
        "snapshotCount": len(all_snapshots),
        "errors":      errors,
        "quota":       events_resp.get("quota"),
    }


def _abbr_from_team_name(team_name: str) -> str:
    """Best-effort convert Odds API team name → NBA abbreviation."""
    if not team_name:
        return ""
    norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(team_name).lower())).strip()
    for abbr, t in _ALL_TEAMS_BY_ABBR.items():
        city     = str(t.get("city",      "") or "").lower().strip()
        nickname = str(t.get("nickname",  "") or "").lower().strip()
        full     = str(t.get("full_name", "") or "").lower().strip()
        for variant in (city, nickname, full, f"{city} {nickname}"):
            if variant and variant == norm:
                return abbr
        for variant in (city, nickname, full):
            if variant and len(variant) >= 4 and variant in norm:
                return abbr
    return ""


def get_todays_games(game_date=None):
    """
    Fetch NBA games for *game_date* (YYYY-MM-DD).  Defaults to today.
    Falls back to tomorrow then yesterday.
    isStale=True when returning yesterday's data so caller can warn the user.
    """
    try:
        today = game_date or datetime.now().strftime("%Y-%m-%d")

        def fetch_games(date_str):
            return scoreboardv3.ScoreboardV3(
                game_date=date_str, league_id="00", timeout=30
            ).get_dict()

        data       = retry_api_call(lambda: fetch_games(today))
        raw_games  = data.get("scoreboard", {}).get("games", [])
        date_used  = today
        is_stale   = False

        if not raw_games and game_date is None:
            tomorrow  = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            time.sleep(API_DELAY)
            data      = retry_api_call(lambda: fetch_games(tomorrow))
            raw_games = data.get("scoreboard", {}).get("games", [])
            date_used = tomorrow

        if not raw_games and game_date is None:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            time.sleep(API_DELAY)
            data      = retry_api_call(lambda: fetch_games(yesterday))
            raw_games = data.get("scoreboard", {}).get("games", [])
            date_used = yesterday
            is_stale  = True

        games = []
        for g in raw_games:
            home     = g.get("homeTeam", {})
            away     = g.get("awayTeam", {})
            leaders  = g.get("gameLeaders", {})
            h_leader = leaders.get("homeLeaders", {})
            a_leader = leaders.get("awayLeaders", {})
            games.append({
                "gameId":      g.get("gameId", ""),
                "gameCode":    g.get("gameCode", ""),
                "status":      g.get("gameStatusText", ""),
                "gameStatus":  g.get("gameStatus", 1),
                "period":      g.get("period", 0),
                "gameTimeUTC": g.get("gameTimeUTC", ""),
                "homeTeam": {
                    "id": home.get("teamId", 0), "name": home.get("teamName", ""),
                    "abbreviation": home.get("teamTricode", ""), "city": home.get("teamCity", ""),
                    "score": home.get("score", 0), "wins": home.get("wins", 0), "losses": home.get("losses", 0),
                },
                "awayTeam": {
                    "id": away.get("teamId", 0), "name": away.get("teamName", ""),
                    "abbreviation": away.get("teamTricode", ""), "city": away.get("teamCity", ""),
                    "score": away.get("score", 0), "wins": away.get("wins", 0), "losses": away.get("losses", 0),
                },
                "homeLeader": {
                    "name": h_leader.get("name", ""), "playerId": h_leader.get("personId", 0),
                    "pts": h_leader.get("points", 0), "reb": h_leader.get("rebounds", 0),
                    "ast": h_leader.get("assists", 0),
                },
                "awayLeader": {
                    "name": a_leader.get("name", ""), "playerId": a_leader.get("personId", 0),
                    "pts": a_leader.get("points", 0), "reb": a_leader.get("rebounds", 0),
                    "ast": a_leader.get("assists", 0),
                },
            })

        return {"success": True, "games": games, "date": date_used, "isStale": is_stale}
    except Exception as e:
        return {"success": False, "error": str(e), "games": [],
                "date": datetime.now().strftime("%Y-%m-%d"), "isStale": False}

def get_all_teams():
    try:
        return {
            "success": True,
            "teams": [
                {"id": t["id"], "name": t["full_name"], "abbreviation": t["abbreviation"],
                 "city": t["city"], "nickname": t["nickname"]}
                for t in nba_teams_static.get_teams()
            ],
        }
    except Exception as e:
        return {"success": False, "error": str(e), "teams": []}

def get_all_players():
    try:
        return {
            "success": True,
            "players": [
                {"id": p["id"], "name": p["full_name"],
                 "firstName": p["first_name"], "lastName": p["last_name"]}
                for p in _ACTIVE_PLAYERS
            ],
        }
    except Exception as e:
        return {"success": False, "error": str(e), "players": []}

def get_player_game_log(player_id, season=None, last_n=25, as_of_date=None):
    """
    Per-game stats with rolling averages and hit rates.

    v2 changes:
    - Hit-rate primary line: floor(avg)+0.5 instead of round(avg-0.5)
      This mirrors actual sportsbook posting and eliminates the systematic
      over-rate inflation present in v1.
    - Alt-line sweep at offsets -3.0, -1.5, 0, +1.5, +3.0 from primary line.
    - underRate added alongside overRate (no pushes on .5 lines).
    """
    cache_key = None
    try:
        if season is None:
            season = CURRENT_SEASON
        cutoff_date = _coerce_date(as_of_date)
        cutoff_key = cutoff_date.isoformat() if cutoff_date else "full"
        last_n_key = "all" if last_n is None else int(last_n)
        cache_key = f"gamelog_{player_id}_{season}_{last_n_key}_{cutoff_key}"
        cached = cache_get(cache_key, _GAMELOG_CACHE_TTL)
        if cached:
            return cached

        date_to_nullable = ""
        if cutoff_date:
            date_to_nullable = (cutoff_date - timedelta(days=1)).strftime("%m/%d/%Y")

        def fetch():
            return playergamelog.PlayerGameLog(
                player_id=player_id, season=season,
                season_type_all_star="Regular Season",
                date_to_nullable=date_to_nullable,
                headers=HEADERS, timeout=30,
            )

        raw = retry_api_call(fetch).get_normalized_dict().get("PlayerGameLog", [])

        result = []
        for g in raw:
            if cutoff_date:
                g_date = _game_date_obj(g.get("GAME_DATE"))
                if g_date and g_date >= cutoff_date:
                    continue
            pts  = g.get("PTS",  0) or 0
            reb  = g.get("REB",  0) or 0
            ast  = g.get("AST",  0) or 0
            stl  = g.get("STL",  0) or 0
            blk  = g.get("BLK",  0) or 0
            tov  = g.get("TOV",  0) or 0
            fg3m = g.get("FG3M", 0) or 0
            mins = g.get("MIN",  0) or 0
            fgm  = g.get("FGM",  0) or 0
            fga  = g.get("FGA",  0) or 0
            ftm  = g.get("FTM",  0) or 0
            fta  = g.get("FTA",  0) or 0
            fg3a = g.get("FG3A", 0) or 0
            matchup  = g.get("MATCHUP", "")
            is_home  = "vs." in matchup
            opponent = matchup.split(" ")[-1] if matchup else ""
            result.append({
                "gameDate": g.get("GAME_DATE", ""), "gameId": g.get("Game_ID", ""),
                "matchup": matchup, "opponent": opponent, "isHome": is_home,
                "wl": g.get("WL", ""),
                "min": mins, "pts": pts, "reb": reb, "ast": ast,
                "stl": stl, "blk": blk, "tov": tov, "fg3m": fg3m,
                "fg3a": fg3a, "fgm": fgm, "fga": fga, "ftm": ftm, "fta": fta,
                "fgPct":  safe_round((g.get("FG_PCT",  0) or 0) * 100, 1),
                "fg3Pct": safe_round((g.get("FG3_PCT", 0) or 0) * 100, 1),
                "ftPct":  safe_round((g.get("FT_PCT",  0) or 0) * 100, 1),
                "plusMinus": g.get("PLUS_MINUS", 0) or 0,
                "pra": pts + reb + ast, "pr": pts + reb,
                "pa":  pts + ast,       "ra": reb + ast,
                "stocksBlkStl": stl + blk,
            })

        if last_n is not None:
            result = result[:max(1, int(last_n))]

        # DNP filter — exclude games with min below threshold (distorts averages)
        dnp_threshold = PROJECTION_CONFIG.get("dnp_min_threshold", 1)
        excluded_games = [g for g in result if (g.get("min") or 0) < dnp_threshold]
        result = [g for g in result if (g.get("min") or 0) >= dnp_threshold]
        games_excluded_dnp = len(excluded_games)

        # Rolling averages
        stat_keys = ["pts", "reb", "ast", "stl", "blk", "tov", "fg3m",
                     "pra", "pr", "pa", "ra", "min"]
        rolling = {}
        for key in stat_keys:
            vals = [g[key] for g in result]
            rolling[f"{key}_avg5"]       = safe_round(statistics.mean(vals[:5]),  1) if len(vals) >= 5  else (safe_round(statistics.mean(vals), 1) if vals else 0)
            rolling[f"{key}_avg10"]      = safe_round(statistics.mean(vals[:10]), 1) if len(vals) >= 10 else (safe_round(statistics.mean(vals), 1) if vals else 0)
            rolling[f"{key}_avg_season"] = safe_round(statistics.mean(vals),      1) if vals else 0
            rolling[f"{key}_median"]     = safe_round(statistics.median(vals),    1) if vals else 0
            rolling[f"{key}_stdev"]      = safe_round(statistics.stdev(vals),     2) if len(vals) >= 2 else 0
            rolling[f"{key}_min"]        = min(vals) if vals else 0
            rolling[f"{key}_max"]        = max(vals) if vals else 0

        # Hit rates – primary line at floor(avg)+0.5, sweep ±1.5 and ±3.0
        hit_rates = {}
        for key in ["pts", "reb", "ast", "fg3m", "pra", "stl", "blk", "tov"]:
            vals = [g[key] for g in result]
            if not vals:
                continue
            avg          = statistics.mean(vals)
            primary_line = math.floor(avg) + 0.5
            alt_lines    = {}
            for offset in [-3.0, -1.5, 0.0, 1.5, 3.0]:
                cl = max(0.5, primary_line + offset)
                over_ct = sum(1 for v in vals if v > cl)
                alt_lines[str(cl)] = safe_round(safe_div(over_ct, len(vals)) * 100, 1)
            over_primary  = sum(1 for v in vals if v > primary_line)
            under_primary = len(vals) - over_primary
            hit_rates[key] = {
                "line":       primary_line,
                "overRate":   safe_round(safe_div(over_primary,  len(vals)) * 100, 1),
                "underRate":  safe_round(safe_div(under_primary, len(vals)) * 100, 1),
                "sampleSize": len(vals),
                "avg":        safe_round(avg, 1),
                "altLines":   alt_lines,
            }

        out = {
            "success": True, "gameLogs": result, "rolling": rolling,
            "hitRates": hit_rates, "playerId": player_id, "gamesPlayed": len(result),
            "gamesExcludedDnp": games_excluded_dnp,
            "excludedGames": [
                {"gameDate": g.get("gameDate", ""), "gameId": g.get("gameId", "")}
                for g in excluded_games
            ],
        }
        cache_set(cache_key, out)
        return out
    except Exception as e:
        fallback, _ = _local_game_log_fallback(
            player_id=player_id,
            season=season,
            last_n=last_n,
            as_of_date=as_of_date,
        )
        if fallback:
            if cache_key:
                cache_set(cache_key, fallback)
            return fallback
        return {"success": False, "error": str(e), "gameLogs": [], "rolling": {},
                "hitRates": {}, "playerId": player_id, "gamesPlayed": 0,
                "gamesExcludedDnp": 0, "excludedGames": []}

def get_player_splits(player_id, season=None, as_of_date=None):
    """
    Fetch home/away, rest-day, and win/loss splits. Disk-cached 15 min.
    restDays keys are the raw GROUP_VALUE strings from the NBA API
    (e.g. "0", "1", "2+") — checked by _rest_adj with multiple fallback keys.
    """
    if season is None:
        season = CURRENT_SEASON
    cutoff_date = _coerce_date(as_of_date)
    cutoff_key = cutoff_date.isoformat() if cutoff_date else "full"
    cache_key = f"splits_{player_id}_{season}_{cutoff_key}"
    cached    = cache_get(cache_key, _SPLITS_CACHE_TTL)
    if cached:
        return cached

    try:
        def fetch():
            date_to_nullable = ""
            if cutoff_date:
                date_to_nullable = (cutoff_date - timedelta(days=1)).strftime("%m/%d/%Y")
            return playerdashboardbygeneralsplits.PlayerDashboardByGeneralSplits(
                player_id=player_id, season=season,
                per_mode_detailed="PerGame",
                date_to_nullable=date_to_nullable,
                timeout=30,
            )

        data = retry_api_call(fetch).get_normalized_dict()

        def extract_split(row):
            return {
                "gp":    row.get("GP", 0),
                "min":   safe_round(row.get("MIN",  0)),
                "pts":   safe_round(row.get("PTS",  0)),
                "reb":   safe_round(row.get("REB",  0)),
                "ast":   safe_round(row.get("AST",  0)),
                "stl":   safe_round(row.get("STL",  0)),
                "blk":   safe_round(row.get("BLK",  0)),
                "tov":   safe_round(row.get("TOV",  0)),
                "fg3m":  safe_round(row.get("FG3M", 0)),
                "fgPct": safe_round((row.get("FG_PCT", 0) or 0) * 100),
                "ftPct": safe_round((row.get("FT_PCT", 0) or 0) * 100),
                "pra":   safe_round(
                    (row.get("PTS", 0) or 0) +
                    (row.get("REB", 0) or 0) +
                    (row.get("AST", 0) or 0)
                ),
            }

        result = {"overall": None, "home": None, "away": None,
                  "restDays": {}, "wins": None, "losses": None}

        overall = data.get("OverallPlayerDashboard", [])
        if overall:
            result["overall"] = extract_split(overall[0])

        for loc in data.get("LocationPlayerDashboard", []):
            gv = loc.get("GROUP_VALUE", "")
            if gv == "Home":   result["home"] = extract_split(loc)
            elif gv == "Road": result["away"] = extract_split(loc)

        for r in data.get("DaysRestPlayerDashboard", []):
            result["restDays"][r.get("GROUP_VALUE", "")] = extract_split(r)

        for w in data.get("WinsLossesPlayerDashboard", []):
            gv = w.get("GROUP_VALUE", "")
            if gv == "Wins":     result["wins"]   = extract_split(w)
            elif gv == "Losses": result["losses"] = extract_split(w)

        out = {"success": True, "splits": result, "playerId": player_id}
        cache_set(cache_key, out)
        return out
    except Exception as e:
        return {"success": False, "error": str(e), "splits": None, "playerId": player_id}

def get_team_defensive_ratings(as_of_date=None):
    """
    Base stats + opponent allowances + advanced (pace, ratings).
    Computes defensive multipliers vs league average per stat.
    Disk-cached 30 min — 3 API calls on cold cache.
    """
    cutoff_date = _coerce_date(as_of_date)
    cutoff_key = cutoff_date.isoformat() if cutoff_date else "full"
    cache_key = f"defense_{CURRENT_SEASON}_{cutoff_key}"
    cached    = cache_get(cache_key, _DEFENSE_CACHE_TTL)
    if cached:
        return cached

    try:
        date_to_nullable = ""
        if cutoff_date:
            date_to_nullable = (cutoff_date - timedelta(days=1)).strftime("%m/%d/%Y")

        def fetch_base():
            return leaguedashteamstats.LeagueDashTeamStats(
                season=CURRENT_SEASON, per_mode_detailed="PerGame",
                season_type_all_star="Regular Season", headers=HEADERS, timeout=30,
                date_to_nullable=date_to_nullable,
            )

        def fetch_opp():
            return leaguedashteamstats.LeagueDashTeamStats(
                season=CURRENT_SEASON, measure_type_detailed_defense="Opponent",
                per_mode_detailed="PerGame", season_type_all_star="Regular Season",
                headers=HEADERS, timeout=30, date_to_nullable=date_to_nullable,
            )

        def fetch_adv():
            return leaguedashteamstats.LeagueDashTeamStats(
                season=CURRENT_SEASON, measure_type_detailed_defense="Advanced",
                per_mode_detailed="PerGame", season_type_all_star="Regular Season",
                headers=HEADERS, timeout=30, date_to_nullable=date_to_nullable,
            )

        base_data = retry_api_call(fetch_base).get_normalized_dict().get("LeagueDashTeamStats", [])
        time.sleep(API_DELAY)
        opp_data  = retry_api_call(fetch_opp).get_normalized_dict().get("LeagueDashTeamStats", [])
        time.sleep(API_DELAY)
        adv_data  = retry_api_call(fetch_adv).get_normalized_dict().get("LeagueDashTeamStats", [])

        opp_map = {o.get("TEAM_ID"): o for o in opp_data}
        adv_map = {a.get("TEAM_ID"): a for a in adv_data}

        # League averages for multiplier normalisation
        league_avg = {}
        if opp_data:
            n = len(opp_data)
            for key in ["OPP_PTS", "OPP_REB", "OPP_AST", "OPP_STL",
                        "OPP_BLK", "OPP_TOV", "OPP_FG3M"]:
                league_avg[key] = sum(o.get(key, 0) or 0 for o in opp_data) / n

        league_pace_avg = (
            sum(x.get("PACE", 100) or 100 for x in adv_data) / max(len(adv_data), 1)
        ) if adv_data else 100.0

        teams_result = []
        for t in base_data:
            team_id = t.get("TEAM_ID")
            o = opp_map.get(team_id, {})
            a = adv_map.get(team_id, {})

            opp_pts  = o.get("OPP_PTS",  0) or 0
            opp_reb  = o.get("OPP_REB",  0) or 0
            opp_ast  = o.get("OPP_AST",  0) or 0
            opp_stl  = o.get("OPP_STL",  0) or 0
            opp_blk  = o.get("OPP_BLK",  0) or 0
            opp_tov  = o.get("OPP_TOV",  0) or 0
            opp_fg3m = o.get("OPP_FG3M", 0) or 0

            def def_mult(stat_key, val):
                avg = league_avg.get(stat_key, 0)
                return safe_round(val / avg, 3) if avg > 0 else 1.0

            pace        = a.get("PACE", 100) or 100
            pace_factor = safe_round(pace / league_pace_avg, 3) if league_pace_avg > 0 else 1.0

            teams_result.append({
                "teamId":       team_id,
                "abbreviation": t.get("TEAM_ABBREVIATION", ""),
                "name":         t.get("TEAM_NAME", ""),
                "gp":    t.get("GP", 0),
                "wins":  t.get("W",  0),
                "losses":t.get("L",  0),
                "winPct":safe_round(t.get("W_PCT", 0), 3),
                # What they allow per game
                "defPtsAllowed":  safe_round(opp_pts),
                "defRebAllowed":  safe_round(opp_reb),
                "defAstAllowed":  safe_round(opp_ast),
                "defStlAllowed":  safe_round(opp_stl),
                "defBlkAllowed":  safe_round(opp_blk),
                "defTovForced":   safe_round(opp_tov),
                "defFg3mAllowed": safe_round(opp_fg3m),
                # Multipliers vs league average (>1.0 = weak defense = good for player)
                "defPtsMult":  def_mult("OPP_PTS",  opp_pts),
                "defRebMult":  def_mult("OPP_REB",  opp_reb),
                "defAstMult":  def_mult("OPP_AST",  opp_ast),
                "defStlMult":  def_mult("OPP_STL",  opp_stl),
                "defBlkMult":  def_mult("OPP_BLK",  opp_blk),
                "defTovMult":  def_mult("OPP_TOV",  opp_tov),
                "defFg3mMult": def_mult("OPP_FG3M", opp_fg3m),
                # Advanced
                "pace":       safe_round(pace),
                "paceFactor": pace_factor,
                "offRtg":     safe_round(a.get("OFF_RATING", 0)),
                "defRtg":     safe_round(a.get("DEF_RATING", 0)),
                "netRtg":     safe_round(a.get("NET_RATING", 0)),
                # Defensive ranks (1 = best/toughest defense)
                "defPtsRank":  o.get("OPP_PTS_RANK",  15),
                "defRebRank":  o.get("OPP_REB_RANK",  15),
                "defAstRank":  o.get("OPP_AST_RANK",  15),
                "defFg3mRank": o.get("OPP_FG3M_RANK", 15),
            })

        out = {"success": True, "teams": teams_result, "leagueAvg": league_avg}
        cache_set(cache_key, out)
        return out
    except Exception as e:
        return {"success": False, "error": str(e), "teams": [], "leagueAvg": {}}

def get_active_players_for_teams(team_ids_str):
    try:
        team_ids = [int(x) for x in team_ids_str.split(",") if x.strip()]

        def fetch():
            return leaguedashplayerstats.LeagueDashPlayerStats(
                season=CURRENT_SEASON, per_mode_detailed="PerGame",
                season_type_all_star="Regular Season", headers=HEADERS, timeout=30,
            )

        player_stats = retry_api_call(fetch).get_normalized_dict().get("LeagueDashPlayerStats", [])

        result = []
        for p in player_stats:
            tid  = p.get("TEAM_ID", 0)
            if team_ids and tid not in team_ids:
                continue
            gp   = p.get("GP",  0) or 0
            mins = p.get("MIN", 0) or 0
            if gp < 3 or mins < 8:
                continue
            pts  = safe_round(p.get("PTS",  0))
            reb  = safe_round(p.get("REB",  0))
            ast  = safe_round(p.get("AST",  0))
            stl  = safe_round(p.get("STL",  0))
            blk  = safe_round(p.get("BLK",  0))
            tov  = safe_round(p.get("TOV",  0))
            fg3m = safe_round(p.get("FG3M", 0))
            result.append({
                "playerId":   p.get("PLAYER_ID", 0),
                "playerName": p.get("PLAYER_NAME", ""),
                "teamId":     tid,
                "teamAbbr":   p.get("TEAM_ABBREVIATION", ""),
                "age":        p.get("AGE", 0),
                "gp":         gp,
                "min":        safe_round(mins),
                "pts": pts, "reb": reb, "ast": ast,
                "stl": stl, "blk": blk, "tov": tov, "fg3m": fg3m,
                "fgPct":  safe_round((p.get("FG_PCT",  0) or 0) * 100),
                "fg3Pct": safe_round((p.get("FG3_PCT", 0) or 0) * 100),
                "ftPct":  safe_round((p.get("FT_PCT",  0) or 0) * 100),
                "pra":  safe_round(pts + reb + ast),
                "pr":   safe_round(pts + reb),
                "pa":   safe_round(pts + ast),
                "ra":   safe_round(reb + ast),
                "fantasyPts": safe_round(p.get("NBA_FANTASY_PTS", 0)),
            })

        result.sort(key=lambda x: x["min"], reverse=True)
        return {"success": True, "players": result}
    except Exception as e:
        return {"success": False, "error": str(e), "players": []}

def get_player_position(player_id):
    """
    Fetch player position. Returns normalised 'G', 'F', or 'C'.
    Disk-cached 24 hours — position almost never changes mid-season.
    """
    cache_key = f"player_pos_{player_id}"
    cached    = cache_get(cache_key, 86400)
    if cached:
        return cached

    try:
        def fetch():
            return commonplayerinfo.CommonPlayerInfo(player_id=player_id, timeout=30)

        info       = retry_api_call(fetch)
        player_info = info.get_normalized_dict().get("CommonPlayerInfo", [{}])[0]
        raw_pos    = player_info.get("POSITION", "") or ""
        pl         = raw_pos.lower()

        if "center" in pl and "forward" not in pl:
            normalized = "C"
        elif "forward" in pl:
            normalized = "F"
        else:
            normalized = "G"   # guard is the safest default

        result = {"position": normalized, "rawPosition": raw_pos, "playerId": player_id}
        cache_set(cache_key, result)
        return result
    except Exception:
        return {"position": "G", "rawPosition": "", "playerId": player_id}

def get_matchup_history(logs, opponent_abbr):
    """
    Extract historical performance vs a specific opponent from existing game logs.
    Returns per-stat avg/stdev/min/max/games, or None if fewer than 2 games found.
    This is free — no API call required.
    """
    matchup_games = [g for g in logs if g.get("opponent", "") == opponent_abbr]
    if len(matchup_games) < 2:
        return None

    stats = ["pts", "reb", "ast", "stl", "blk", "tov",
             "fg3m", "pra", "pr", "pa", "ra", "min"]
    result = {}
    for stat in stats:
        vals = [g[stat] for g in matchup_games]
        result[stat] = {
            "avg":   safe_round(statistics.mean(vals), 1),
            "games": len(vals),
            "min":   min(vals),
            "max":   max(vals),
            "stdev": safe_round(statistics.stdev(vals), 2) if len(vals) >= 2 else 0,
        }
    return result

def get_yesterdays_team_abbrs(date_str: str = None) -> set:
    """
    Return the set of team abbreviations that played YESTERDAY relative to date_str.
    Used for back-to-back detection: if a team played yesterday they are on a B2B today.
    Uses NBA Stats API scoreboardv3 (free — no Odds API credits).
    Falls back to empty set on any error.
    """
    try:
        from datetime import datetime, timedelta
        ds = date_str or datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.strptime(ds, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        data = retry_api_call(
            lambda: scoreboardv3.ScoreboardV3(
                game_date=yesterday, league_id="00", timeout=30
            ).get_dict()
        )
        abbrs = set()
        for g in (data.get("scoreboard", {}).get("games", []) or []):
            ht = (g.get("homeTeam") or {}).get("teamTricode", "")
            at = (g.get("awayTeam") or {}).get("teamTricode", "")
            if ht: abbrs.add(ht.upper())
            if at: abbrs.add(at.upper())
        return abbrs
    except Exception:
        return set()


def get_todays_game_totals(date_str: str = None) -> dict:
    """
    Fetch O/U game totals for all of today's NBA games via Odds API totals market.
    Returns dict keyed by frozenset({home_abbr, away_abbr}) -> float.
    One API call covers all games. Falls back to {} on error or missing key.
    Season average is ~226; deviations drive pts projection multiplier.
    """
    try:
        resp = _odds_api_get(
            "sports/basketball_nba/odds",
            params={
                "regions": ODDS_DEFAULT_REGIONS,
                "markets": "totals",
                "oddsFormat": "american",
                "dateFormat": "iso",
            },
        )
        if not resp.get("success"):
            return {}
        totals = {}
        for event in (resp.get("data") or []):
            home_name = event.get("home_team", "")
            away_name = event.get("away_team", "")
            home_abbr = _abbr_from_team_name(home_name)
            away_abbr = _abbr_from_team_name(away_name)
            if not home_abbr or not away_abbr:
                continue
            # Take the first bookmaker that has a totals market
            total_pt = None
            for bm in (event.get("bookmakers") or []):
                for mkt in (bm.get("markets") or []):
                    if mkt.get("key") != "totals":
                        continue
                    for outcome in (mkt.get("outcomes") or []):
                        if str(outcome.get("name", "")).lower() == "over":
                            total_pt = float(outcome["point"])
                            break
                    if total_pt is not None:
                        break
                if total_pt is not None:
                    break
            if total_pt is not None:
                totals[frozenset({home_abbr, away_abbr})] = total_pt
        return totals
    except Exception:
        return {}


_player_team_cache = {"data": None, "ts": 0}

def get_player_team_map(max_age_sec: int = 3600) -> dict:
    """
    Return {normalized_player_name: TEAM_ABBR} for all current-season players.
    Uses LeagueDashPlayerStats (one API call, 544 rows). Cached for max_age_sec.
    """
    now = time.time()
    if _player_team_cache["data"] and (now - _player_team_cache["ts"]) < max_age_sec:
        return _player_team_cache["data"]
    try:
        time.sleep(API_DELAY)
        result = retry_api_call(
            lambda: leaguedashplayerstats.LeagueDashPlayerStats(
                season=CURRENT_SEASON, timeout=30,
            )
        )
        rows = result.get_normalized_dict().get("LeagueDashPlayerStats", [])
        mapping = {}
        for r in rows:
            name = re.sub(r"[.\-'']", "", str(r.get("PLAYER_NAME", ""))).lower().strip()
            abbr = str(r.get("TEAM_ABBREVIATION", "")).upper()
            if name and abbr:
                mapping[name] = abbr
        _player_team_cache["data"] = mapping
        _player_team_cache["ts"] = now
        return mapping
    except Exception:
        return _player_team_cache["data"] or {}


def validate_player_team(player_name, claimed_team_abbr, event_home, event_away):
    """Check if player belongs to either team in this event.

    Returns (actual_team_abbr, is_valid).
    Rules (in order):
      1. If actual team is known and differs from claimed → reject (trade detected)
      2. If claimed team not in event → reject
      3. If actual team known and not in event → reject
      4. If no claimed team and can't resolve → reject (missing data)
    """
    claimed = str(claimed_team_abbr or "").upper()
    home = str(event_home or "").upper()
    away = str(event_away or "").upper()
    event_teams = {home, away} - {""}

    norm = re.sub(r"[.\-\u2019']", "", str(player_name or "")).lower().strip()
    ptm = get_player_team_map()
    actual = ptm.get(norm, "")

    # Rule 1: if we know the player's actual team and it differs from claimed, reject
    if actual and claimed and actual.upper() != claimed.upper():
        return actual, False

    # Rule 2: claimed team must be in the event
    if claimed and event_teams and claimed not in event_teams:
        return actual or claimed, False

    # Rule 3: if actual team known but not in event, reject
    if actual and event_teams and actual.upper() not in event_teams:
        return actual, False

    # Rule 4: no claimed team and can't resolve
    if not claimed and not actual:
        return "", False

    return actual or claimed, True


def get_game_total(home_abbr: str, away_abbr: str, date_str: str = None) -> float | None:
    """
    Convenience wrapper: look up a single game's O/U total.
    Returns float or None. Uses get_todays_game_totals() internally.
    """
    totals = get_todays_game_totals(date_str)
    return totals.get(frozenset({home_abbr.upper(), away_abbr.upper()}))


def get_position_vs_team(opponent_team_id, season=None, as_of_date=None):
    """
    How do players collectively perform against this specific team vs their
    season averages? Computes independent per-stat defensive multipliers by:
      1. Fetching all players' stats vs this opponent (vs_team_id filter)
      2. Fetching all players' season averages
      3. Computing ratio: vs_opponent / season_avg for each eligible player
      4. Trimming top/bottom 10% outliers and averaging
      5. Capping results at [0.60, 1.55]

    Provides an independent corroboration of team-level defense multipliers.
    Disk-cached 30 min.
    """
    if season is None:
        season = CURRENT_SEASON
    cutoff_date = _coerce_date(as_of_date)
    cutoff_key = cutoff_date.isoformat() if cutoff_date else "full"

    cache_key = f"pvt_{opponent_team_id}_{season}_{cutoff_key}"
    cached    = cache_get(cache_key, _PVT_CACHE_TTL)
    if cached:
        return cached

    try:
        date_to_nullable = ""
        if cutoff_date:
            date_to_nullable = (cutoff_date - timedelta(days=1)).strftime("%m/%d/%Y")

        def fetch_vs():
            return leaguedashplayerstats.LeagueDashPlayerStats(
                season=season, per_mode_detailed="PerGame",
                season_type_all_star="Regular Season",
                vs_team_id=str(opponent_team_id),
                headers=HEADERS, timeout=30, date_to_nullable=date_to_nullable,
            )

        def fetch_all():
            return leaguedashplayerstats.LeagueDashPlayerStats(
                season=season, per_mode_detailed="PerGame",
                season_type_all_star="Regular Season",
                headers=HEADERS, timeout=30, date_to_nullable=date_to_nullable,
            )

        vs_raw  = retry_api_call(fetch_vs).get_normalized_dict().get("LeagueDashPlayerStats", [])
        time.sleep(API_DELAY)
        all_raw = retry_api_call(fetch_all).get_normalized_dict().get("LeagueDashPlayerStats", [])

        all_map   = {p["PLAYER_ID"]: p for p in all_raw}
        qualified = [
            p for p in vs_raw
            if (p.get("GP",  0) or 0) >= 2
            and (p.get("MIN", 0) or 0) >= 15
        ]

        if len(qualified) < 5:
            result = {"success": False, "multipliers": {}, "sampleSize": len(qualified),
                      "reason": "Insufficient sample size (<5 qualified players)"}
            cache_set(cache_key, result)
            return result

        STAT_KEYS = [
            ("PTS", "pts"), ("REB", "reb"), ("AST", "ast"),
            ("STL", "stl"), ("BLK", "blk"), ("TOV", "tov"), ("FG3M", "fg3m"),
        ]
        multipliers = {}
        for api_key, stat_key in STAT_KEYS:
            ratios = []
            for p in qualified:
                pid        = p.get("PLAYER_ID")
                league_p   = all_map.get(pid) or {}
                vs_val     = p.get(api_key,    0) or 0
                league_val = league_p.get(api_key, 0) or 0
                if league_val > 0.5:
                    ratios.append(vs_val / league_val)

            if ratios:
                ratios.sort()
                trim    = max(1, len(ratios) // 10)
                trimmed = ratios[trim:-trim] if len(ratios) > 2 * trim else ratios
                mult    = statistics.mean(trimmed)
                multipliers[stat_key] = safe_round(max(0.60, min(1.55, mult)), 3)
            else:
                multipliers[stat_key] = 1.0

        result = {
            "success":        True,
            "multipliers":    multipliers,
            "sampleSize":     len(qualified),
            "opponentTeamId": opponent_team_id,
        }
        cache_set(cache_key, result)
        return result
    except Exception as e:
        return {"success": False, "multipliers": {}, "error": str(e)}

def get_team_roster_status(team_abbr, season=None):
    """
    Assess availability of every player on a team by cross-referencing:
      1. Full-season stats (leaguedashplayerstats) — who is on the roster
      2. Last-5-game stats — who has actually been playing recently
      3. Common team roster (commonteamroster) — full official roster incl. inactive

    A player is classified as:
      "Active"          — played at least once in last 5 games
      "Likely Inactive" — on roster, played ≥10 games this season, but 0 in last 5
      "Inactive"        — on official roster but 0 GP all season (G-League / IL)

    Returns each player with name, playerId, position, usgPct, seasonGP,
    recentGP, recentMin, status, and riskLevel (High/Medium/Low based on USG%).

    Disk-cached 15 min.
    """
    if season is None:
        season = CURRENT_SEASON

    team_id = team_id_from_abbr(team_abbr)
    if not team_id:
        return {"success": False, "error": f"Unknown team abbreviation: {team_abbr}"}

    cache_key = f"roster_status_{team_id}_{season}"
    cached    = cache_get(cache_key, 900)
    if cached:
        return cached

    try:
        # 1. Full-season player stats for this team
        def fetch_season():
            return leaguedashplayerstats.LeagueDashPlayerStats(
                season=season,
                per_mode_detailed="PerGame",
                season_type_all_star="Regular Season",
                team_id_nullable=str(team_id),
                headers=HEADERS,
                timeout=30,
            )

        time.sleep(API_DELAY)
        season_raw = retry_api_call(fetch_season).get_normalized_dict().get("LeagueDashPlayerStats", [])

        # 1b. Advanced stats for reliable USG_PCT (Base table may omit it)
        def fetch_advanced():
            return leaguedashplayerstats.LeagueDashPlayerStats(
                season=season,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="PerGame",
                season_type_all_star="Regular Season",
                team_id_nullable=str(team_id),
                headers=HEADERS,
                timeout=30,
            )

        try:
            time.sleep(API_DELAY)
            advanced_raw = retry_api_call(fetch_advanced).get_normalized_dict().get("LeagueDashPlayerStats", [])
        except Exception:
            advanced_raw = []

        # 2. Last-5-game stats for this team
        def fetch_recent():
            return leaguedashplayerstats.LeagueDashPlayerStats(
                season=season,
                per_mode_detailed="PerGame",
                season_type_all_star="Regular Season",
                last_n_games=5,
                team_id_nullable=str(team_id),
                headers=HEADERS,
                timeout=30,
            )

        time.sleep(API_DELAY)
        recent_raw = retry_api_call(fetch_recent).get_normalized_dict().get("LeagueDashPlayerStats", [])

        # 3. Official roster (includes players on IL / inactive)
        def fetch_roster():
            return commonteamroster.CommonTeamRoster(
                team_id=str(team_id),
                season=season,
                timeout=30,
            )

        time.sleep(API_DELAY)
        roster_raw  = retry_api_call(fetch_roster).get_normalized_dict().get("CommonTeamRoster", [])

        def _player_key(raw_id):
            try:
                return int(raw_id)
            except Exception:
                return raw_id

        def _normalize_pct(raw_val):
            try:
                val = float(raw_val)
            except (TypeError, ValueError):
                return 0.0
            # Some payloads return 0-1 while others return 0-100.
            if val <= 1.0:
                val *= 100.0
            return safe_round(max(0.0, min(100.0, val)), 1)

        # Build lookups
        season_map   = {_player_key(p.get("PLAYER_ID")): p for p in season_raw}
        recent_map   = {_player_key(p.get("PLAYER_ID")): p for p in recent_raw}
        advanced_map = {_player_key(p.get("PLAYER_ID")): p for p in advanced_raw}

        players_out = []
        for r in roster_raw:
            pid  = _player_key(r.get("PLAYER_ID") or r.get("PlayerID"))
            name = r.get("PLAYER") or r.get("PLAYER_NAME", "Unknown")
            pos  = r.get("POSITION", "")

            season_p = season_map.get(pid, {})
            recent_p = recent_map.get(pid, {})
            adv_p    = advanced_map.get(pid, {})

            season_gp  = season_p.get("GP",  0) or 0
            recent_gp  = recent_p.get("GP",  0) or 0
            recent_min = safe_round(recent_p.get("MIN", 0) or 0)
            usg_raw    = adv_p.get("USG_PCT")
            if usg_raw is None:
                usg_raw = season_p.get("USG_PCT")
            usg_pct    = _normalize_pct(usg_raw)
            season_min = safe_round(season_p.get("MIN", 0) or 0)
            season_pts = safe_round(season_p.get("PTS", 0) or 0)

            # Classify availability
            if season_gp == 0:
                status = "Inactive"
            elif recent_gp == 0 and season_gp >= 10:
                status = "Likely Inactive"
            elif recent_gp == 0 and season_gp > 0:
                status = "Questionable"
            else:
                status = "Active"

            # Risk level: how much does their absence hurt the rest of the team?
            if usg_pct >= 25:
                risk = "High"
            elif usg_pct >= 18:
                risk = "Medium"
            else:
                risk = "Low"

            players_out.append({
                "playerId":    pid,
                "name":        name,
                "position":    pos,
                "status":      status,
                "riskLevel":   risk,
                "usgPct":      usg_pct,
                "seasonGP":    season_gp,
                "recentGP":    recent_gp,
                "recentMin":   recent_min,
                "seasonMin":   season_min,
                "seasonPts":   season_pts,
            })

        # Sort: Likely Inactive first (most actionable), then by USG%
        STATUS_ORDER = {"Likely Inactive": 0, "Questionable": 1, "Inactive": 2, "Active": 3}
        players_out.sort(key=lambda x: (STATUS_ORDER.get(x["status"], 9), -x["usgPct"]))

        # Summary counts
        inactive_count = sum(1 for p in players_out if p["status"] in ("Likely Inactive", "Inactive"))
        high_risk_out  = [p for p in players_out if p["status"] != "Active" and p["riskLevel"] == "High"]

        out = {
            "success":      True,
            "teamAbbr":     team_abbr,
            "teamId":       team_id,
            "players":      players_out,
            "inactiveCount": inactive_count,
            "highRiskOut":  high_risk_out,
            "season":       season,
        }
        cache_set(cache_key, out)
        return out

    except Exception as e:
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}


def _parse_live_minutes(raw):
    """Parse a minutes string from boxscoretraditionalv3 (mm:ss or PT##M##.##S)."""
    s = str(raw or "").strip()
    if not s or s.upper().startswith("DNP"):
        return 0.0
    if s.upper().startswith("PT"):
        m = re.match(r"PT(\d+)M([\d.]+)S", s.upper())
        if m:
            return float(m.group(1)) + float(m.group(2)) / 60.0
        return 0.0
    if ":" in s:
        try:
            mm, ss = s.split(":", 1)
            return max(0.0, float(mm) + float(ss) / 60.0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return max(0.0, float(s))
    except (TypeError, ValueError):
        return 0.0


def get_live_player_stats(player_id, team_abbr):
    """
    Fetch current in-game stats for a player in a live or recently started game today.
    Returns the player's current stat line, minutes played, period, and game metadata.
    """
    try:
        player_id = int(player_id)
        team_abbr = str(team_abbr).upper().strip()

        games_data = get_todays_games()
        games = games_data.get("games", [])

        matching_games = []
        for g in games:
            home_abbr = (g.get("homeTeam") or {}).get("abbreviation", "")
            away_abbr = (g.get("awayTeam") or {}).get("abbreviation", "")
            if team_abbr in (home_abbr, away_abbr):
                matching_games.append(g)

        if not matching_games:
            return {"success": False, "error": f"No game found today for {team_abbr}"}

        # Scoreboard status convention:
        # 1 = scheduled, 2 = live, 3 = final
        selected_game = None
        for g in matching_games:
            try:
                status = int(g.get("gameStatus", 0) or 0)
            except (TypeError, ValueError):
                status = 0
            if status in (2, 3):
                selected_game = g
                break

        if not selected_game:
            g = matching_games[0]
            return {
                "success": False,
                "error": (
                    f"Game for {team_abbr} is not live yet. "
                    "Live projection is available once the game starts."
                ),
                "gameId": g.get("gameId"),
                "period": g.get("period", 0),
                "gameStatus": g.get("gameStatus", 0),
            }

        game_id = selected_game.get("gameId")
        game_period = selected_game.get("period", 0)
        game_status = selected_game.get("gameStatus", 0)
        home_team = selected_game.get("homeTeam") or {}
        away_team = selected_game.get("awayTeam") or {}
        is_home_team = str(home_team.get("abbreviation", "")).upper() == team_abbr
        team_score = float((home_team if is_home_team else away_team).get("score", 0) or 0)
        opp_score = float((away_team if is_home_team else home_team).get("score", 0) or 0)
        score_margin = team_score - opp_score

        def fetch():
            resp = requests.get(
                f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json",
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

        payload = retry_api_call(fetch) or {}
        box = payload.get("game") or {}

        total_players = 0
        for team_key in ("homeTeam", "awayTeam"):
            team = box.get(team_key, {}) or {}
            total_players += len(team.get("players", []) or [])

        if total_players == 0:
            return {
                "success": False,
                "error": "Live boxscore is not populated yet for this game.",
                "gameId": game_id,
                "period": game_period,
                "gameStatus": game_status,
            }

        player_row = None
        for team_key in ("homeTeam", "awayTeam"):
            team = box.get(team_key, {}) or {}
            for player in team.get("players", []) or []:
                if int(player.get("personId", 0) or 0) == player_id:
                    stats = player.get("statistics", {}) or {}
                    pts  = float(stats.get("points") or 0)
                    reb  = float(stats.get("reboundsTotal") or 0)
                    ast  = float(stats.get("assists") or 0)
                    fga = float(stats.get("fieldGoalsAttempted") or 0)
                    fgm = float(stats.get("fieldGoalsMade") or 0)
                    fta = float(stats.get("freeThrowsAttempted") or 0)
                    ftm = float(stats.get("freeThrowsMade") or 0)
                    fg3a = float(stats.get("threePointersAttempted") or 0)
                    fg3m = float(stats.get("threePointersMade") or 0)
                    foul_count = float(stats.get("foulsPersonal") or 0)
                    player_row = {
                        "minsPlayed": _parse_live_minutes(stats.get("minutes", "")),
                        "PTS":  pts,
                        "REB":  reb,
                        "AST":  ast,
                        "STL":  float(stats.get("steals") or 0),
                        "BLK":  float(stats.get("blocks") or 0),
                        "TOV":  float(stats.get("turnovers") or 0),
                        "FG3M": fg3m,
                        "FGA":  fga,
                        "FGM":  fgm,
                        "FTA":  fta,
                        "FTM":  ftm,
                        "FG3A": fg3a,
                        "PF":   foul_count,
                        "ShotAttempts": fga + 0.44 * fta,
                        "PRA":  pts + reb + ast,
                    }
                    break
            if player_row:
                break

        if not player_row:
            return {
                "success": False,
                "error": f"Player {player_id} not found in live boxscore for game {game_id}",
            }

        return {
            "success": True,
            "gameId": game_id,
            "period": game_period,
            "gameStatus": game_status,
            "teamScore": safe_round(team_score, 1),
            "oppScore": safe_round(opp_score, 1),
            "scoreMargin": safe_round(score_margin, 1),
            "stats": player_row,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "traceback": traceback.format_exc()}
