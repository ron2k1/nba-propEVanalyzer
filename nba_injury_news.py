#!/usr/bin/env python3
"""News-driven injury signal ingestion and usage-adjustment overlay."""

import os
import re
from datetime import datetime, timedelta

import requests
from nba_api.stats.static import teams as nba_teams_static

from nba_data_collection import cache_get, cache_set, get_team_roster_status, safe_round
from nba_data_prep import compute_usage_adjustment, _USG_STAT_ELASTICITY

NEWS_API_BASE_URL = "https://newsapi.org/v2/everything"

_TEAM_NAME_BY_ABBR = {
    str(t.get("abbreviation", "")).upper(): str(t.get("full_name", ""))
    for t in nba_teams_static.get_teams()
}

_STATUS_RULES = [
    ("Out", 0.92, [r"\bruled out\b", r"\bout\b", r"\bwill miss\b", r"\binactive\b", r"\bwon'?t play\b"]),
    ("Doubtful", 0.78, [r"\bdoubtful\b"]),
    ("Questionable", 0.64, [r"\bquestionable\b", r"\bgame[- ]time decision\b", r"\bgtd\b"]),
    ("Minutes Watch", 0.58, [r"\bminutes restriction\b", r"\blimited minutes\b"]),
    ("Probable", 0.45, [r"\bprobable\b"]),
]

_SOURCE_BOOST = {
    "nba.com": 0.08,
    "espn": 0.06,
    "the athletic": 0.05,
    "rotowire": 0.05,
    "underdog": 0.04,
}


def _normalize_text(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower())).strip()


def _extract_status_and_confidence(text):
    raw = str(text or "")
    low = raw.lower()
    for status, base_conf, patterns in _STATUS_RULES:
        for pattern in patterns:
            if re.search(pattern, low):
                return status, base_conf
    return None, None


def _source_confidence_boost(source_name):
    name = _normalize_text(source_name)
    if not name:
        return 0.0
    for token, boost in _SOURCE_BOOST.items():
        if token in name:
            return boost
    return 0.0


def fetch_nba_injury_news(team_abbr, lookback_hours=24, max_articles=50):
    """
    Pull NBA injury-related news and map to team roster players.
    Requires NEWS_API_KEY env var.
    """
    team = str(team_abbr or "").upper().strip()
    if not team:
        return {"success": False, "error": "team_abbr required"}

    team_name = _TEAM_NAME_BY_ABBR.get(team)
    if not team_name:
        return {"success": False, "error": f"Unknown team abbreviation: {team}"}

    api_key = os.getenv("NEWS_API_KEY", "").strip()
    if not api_key:
        return {
            "success": False,
            "error": "NEWS_API_KEY not set. Add a key (newsapi.org) to enable injury/news ingestion.",
        }

    cache_key = f"injury_news_{team}_{int(lookback_hours)}_{int(max_articles)}"
    cached = cache_get(cache_key, 300)
    if cached:
        return cached

    roster = get_team_roster_status(team)
    if not roster.get("success"):
        return {"success": False, "error": roster.get("error", "Failed to fetch roster status")}

    roster_players = roster.get("players", []) or []
    roster_norm = { _normalize_text(p.get("name")): p for p in roster_players if p.get("name") }
    if not roster_norm:
        return {"success": False, "error": f"No roster players found for {team}"}

    now_utc = datetime.utcnow()
    from_time = (now_utc - timedelta(hours=max(1, int(lookback_hours)))).replace(microsecond=0).isoformat() + "Z"
    query = (
        f'"{team_name}" AND (NBA) AND '
        f'(injury OR questionable OR doubtful OR probable OR "ruled out" OR inactive)'
    )
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": max(10, min(int(max_articles), 100)),
        "from": from_time,
        "apiKey": api_key,
    }

    try:
        resp = requests.get(NEWS_API_BASE_URL, params=params, timeout=30)
        if resp.status_code != 200:
            return {
                "success": False,
                "error": f"News API HTTP {resp.status_code}",
                "details": (resp.text or "")[:500],
            }
        payload = resp.json()
    except Exception as e:
        return {"success": False, "error": str(e)}

    articles = payload.get("articles", []) or []
    signals = []
    for article in articles:
        title = str(article.get("title", "") or "")
        desc = str(article.get("description", "") or "")
        content = str(article.get("content", "") or "")
        source = str((article.get("source") or {}).get("name", "") or "")
        text = " ".join([title, desc, content]).strip()
        if not text:
            continue

        status, base_conf = _extract_status_and_confidence(text)
        if not status:
            continue
        conf = min(0.99, base_conf + _source_confidence_boost(source))

        norm_text = _normalize_text(text)
        for norm_name, roster_p in roster_norm.items():
            if len(norm_name) < 4:
                continue
            if norm_name in norm_text:
                signals.append({
                    "playerId": roster_p.get("playerId"),
                    "playerName": roster_p.get("name"),
                    "teamAbbr": team,
                    "status": status,
                    "confidence": safe_round(conf, 3),
                    "source": source,
                    "title": title,
                    "url": article.get("url"),
                    "publishedAt": article.get("publishedAt"),
                })

    # Deduplicate by player/status/url
    dedup = {}
    for s in signals:
        key = (
            int(s.get("playerId") or 0),
            str(s.get("status", "")),
            str(s.get("url", "")),
        )
        prev = dedup.get(key)
        if prev is None or float(s.get("confidence", 0)) > float(prev.get("confidence", 0)):
            dedup[key] = s

    signals_out = sorted(
        dedup.values(),
        key=lambda x: (float(x.get("confidence", 0)), str(x.get("publishedAt", ""))),
        reverse=True,
    )

    out = {
        "success": True,
        "teamAbbr": team,
        "teamName": team_name,
        "lookbackHours": int(lookback_hours),
        "articleCount": len(articles),
        "signalCount": len(signals_out),
        "signals": signals_out,
    }
    cache_set(cache_key, out)
    return out


def compute_usage_adjustment_with_news(player_id, team_abbr, season=None, lookback_hours=24):
    """
    Layer injury/news signals onto existing usage adjustment logic.
    """
    base = compute_usage_adjustment(player_id, team_abbr, season=season)
    if not base.get("success"):
        return base

    news = fetch_nba_injury_news(team_abbr, lookback_hours=lookback_hours)
    if not news.get("success"):
        return {
            **base,
            "newsSignals": [],
            "newsStatus": "unavailable",
            "newsError": news.get("error"),
        }

    target_pid = int(player_id)
    impactful = []
    bump = 0.0
    for sig in news.get("signals", []) or []:
        pid = int(sig.get("playerId") or 0)
        if pid == target_pid:
            continue
        status = str(sig.get("status", ""))
        conf = float(sig.get("confidence") or 0.0)
        if status == "Out" and conf >= 0.70:
            impactful.append(sig)
            bump += 0.03
        elif status == "Doubtful" and conf >= 0.70:
            impactful.append(sig)
            bump += 0.02
        elif status == "Questionable" and conf >= 0.70:
            impactful.append(sig)
            bump += 0.01

    bump = min(0.12, bump)
    news_mult = 1.0 + bump

    base_mults = dict(base.get("statMultipliers") or {})
    boosted_mults = {}
    for stat, base_mult in base_mults.items():
        elasticity = float(_USG_STAT_ELASTICITY.get(stat, 0.5))
        news_stat_mult = news_mult ** elasticity
        boosted_mults[stat] = safe_round(float(base_mult) * news_stat_mult, 3)

    out = {
        **base,
        "statMultipliersBase": base_mults,
        "statMultipliers": boosted_mults,
        "newsSignals": impactful,
        "newsSignalCount": len(impactful),
        "newsLookbackHours": int(lookback_hours),
        "newsUsageBump": safe_round(bump, 3),
        "newsUsageMultiplier": safe_round(news_mult, 3),
        "newsStatus": "ok",
    }

    if out.get("estimatedNewUsgPct") is not None:
        out["estimatedNewUsgPctBase"] = out.get("estimatedNewUsgPct")
        out["estimatedNewUsgPct"] = safe_round(float(out.get("estimatedNewUsgPct")) * news_mult, 2)

    return out

