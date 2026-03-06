#!/usr/bin/env python3
"""
One-time enrichment: read lean_bets.jsonl, look up closing/opening lines
from odds_history.sqlite, compute CLV/OLV deltas, write lean_bets_clv.jsonl.

Uses multi-layer name matching:
  1. Normalized exact match (strips periods, diacritics, suffixes)
  2. Alias table (nicknames, legal name changes)
  3. Last-name fallback (unique last names only)

Usage:
  python scripts/enrich_leans_clv.py [--dry-run]
"""

import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.nba_odds_store import STAT_TO_MARKET
from core.nba_bet_tracking import _clv_line_delta, _clv_odds_pct, _as_float

LEAN_BETS_PATH = ROOT / "data" / "lean_bets.jsonl"
OUTPUT_PATH = ROOT / "data" / "lean_bets_clv.jsonl"
ODDS_DB_PATH = ROOT / "data" / "reference" / "odds_history" / "odds_history.sqlite"

# ---------------------------------------------------------------------------
# Name normalization + aliases
# ---------------------------------------------------------------------------

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _norm_name(name):
    """Normalize a player name: strip diacritics, periods, suffixes, collapse initials."""
    name = unicodedata.normalize("NFKD", str(name or ""))
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    name = re.sub(r"\s+", " ", name).strip()
    # Collapse adjacent single-letter tokens into one token: "c j" -> "cj"
    toks = name.split()
    merged = []
    i = 0
    while i < len(toks):
        if len(toks[i]) == 1 and i + 1 < len(toks) and len(toks[i + 1]) == 1:
            # Merge consecutive single chars
            run = toks[i]
            while i + 1 < len(toks) and len(toks[i + 1]) == 1:
                i += 1
                run += toks[i]
            merged.append(run)
        else:
            merged.append(toks[i])
        i += 1
    return " ".join(t for t in merged if t not in _SUFFIXES)


# Map: normalized lean name -> normalized Odds API name
# Covers nicknames, legal name changes, abbreviated first names
_NAME_ALIASES = {
    _norm_name(k): _norm_name(v)
    for k, v in {
        # Periods / initials
        "CJ McCollum": "C.J. McCollum",
        "RJ Barrett": "R.J. Barrett",
        "AJ Green": "A.J. Green",
        "GG Jackson": "G.G. Jackson",
        "PJ Washington": "P.J. Washington",
        "TJ McConnell": "T.J. McConnell",
        "OG Anunoby": "O.G. Anunoby",
        "KJ Martin": "K.J. Martin",
        "JT Thor": "J.T. Thor",
        "EJ Liddell": "E.J. Liddell",
        "AJ Johnson": "A.J. Johnson",
        "TJ Warren": "T.J. Warren",
        "DJ Carton": "D.J. Carton",
        # Nicknames / short names
        "Nic Claxton": "Nicolas Claxton",
        "Moe Wagner": "Moritz Wagner",
        "Bub Carrington": "Carlton Carrington",
        "Ron Holland": "Ronald Holland",
        "Trey Murphy": "Trey Murphy III",
        # Common variations
        "Naz Reid": "Naz Reid",
        "Lu Dort": "Luguentz Dort",
        "Herb Jones": "Herbert Jones",
        "Cam Thomas": "Cameron Thomas",
        "Cam Johnson": "Cameron Johnson",
        "Cam Payne": "Cameron Payne",
        "Pat Connaughton": "Patrick Connaughton",
        "Jabari Walker": "Jabari Walker",
        "Svi Mykhailiuk": "Sviatoslav Mykhailiuk",
        "Ish Wainright": "Ishmail Wainright",
        "Mo Bamba": "Mohamed Bamba",
    }.items()
}

# Build reverse aliases too (Odds API name -> lean name)
_REVERSE_ALIASES = {v: k for k, v in _NAME_ALIASES.items()}


def _build_closing_index(conn):
    """
    Load all closing lines into a multi-key dict for fast lookup.
    Keys: (market, normalized_name, nba_date)
    """
    from datetime import datetime, timedelta

    cur = conn.execute(
        "SELECT market, player_name, close_line, close_over_odds, close_under_odds, "
        "commence_time FROM closing_lines"
    )
    index = {}
    last_name_counts = {}  # track last name uniqueness per (market, date)

    for market, player_name, close_line, close_over, close_under, commence in cur:
        nba_date = None
        if commence and len(commence) >= 10:
            try:
                ct = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                nba_date = (ct - timedelta(hours=6)).strftime("%Y-%m-%d")
            except Exception:
                nba_date = commence[:10]

        if not nba_date or not player_name:
            continue

        entry = {
            "close_line": close_line,
            "close_over_odds": close_over,
            "close_under_odds": close_under,
        }

        norm = _norm_name(player_name)

        # Primary: normalized name
        key = (market, norm, nba_date)
        if key not in index:
            index[key] = entry

        # Track last name frequency for disambiguation
        last = norm.split()[-1] if norm else ""
        if last and len(last) > 2:
            freq_key = (market, last, nba_date)
            last_name_counts[freq_key] = last_name_counts.get(freq_key, 0) + 1

    # Add last-name keys ONLY where the last name is unique for that market+date
    # Re-scan to add unique last names
    cur2 = conn.execute(
        "SELECT market, player_name, close_line, close_over_odds, close_under_odds, "
        "commence_time FROM closing_lines"
    )
    for market, player_name, close_line, close_over, close_under, commence in cur2:
        nba_date = None
        if commence and len(commence) >= 10:
            try:
                ct = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                nba_date = (ct - timedelta(hours=6)).strftime("%Y-%m-%d")
            except Exception:
                nba_date = commence[:10]
        if not nba_date or not player_name:
            continue

        norm = _norm_name(player_name)
        last = norm.split()[-1] if norm else ""
        if last and len(last) > 2:
            freq_key = (market, last, nba_date)
            if last_name_counts.get(freq_key, 0) == 1:
                entry = {
                    "close_line": close_line,
                    "close_over_odds": close_over,
                    "close_under_odds": close_under,
                }
                key = (market, f"__last__{last}", nba_date)
                if key not in index:
                    index[key] = entry

    return index


def _lookup_closing(index, market, player_name, date_str):
    """Multi-layer lookup: normalized -> alias -> unique last name."""
    norm = _norm_name(player_name)

    # Layer 1: normalized exact
    key = (market, norm, date_str)
    if key in index:
        return index[key]

    # Layer 2: alias table
    alias = _NAME_ALIASES.get(norm)
    if alias:
        key2 = (market, alias, date_str)
        if key2 in index:
            return index[key2]

    # Layer 2b: reverse alias (Odds API name -> lean name)
    rev_alias = _REVERSE_ALIASES.get(norm)
    if rev_alias:
        key2b = (market, rev_alias, date_str)
        if key2b in index:
            return index[key2b]

    # Layer 3: unique last name
    last = norm.split()[-1] if norm else ""
    if last and len(last) > 2:
        key3 = (market, f"__last__{last}", date_str)
        if key3 in index:
            return index[key3]

    return None


def main():
    dry_run = "--dry-run" in sys.argv

    if not LEAN_BETS_PATH.exists():
        print(json.dumps({"success": False, "error": f"{LEAN_BETS_PATH} not found."}))
        return
    if not ODDS_DB_PATH.exists():
        print(json.dumps({"success": False, "error": f"{ODDS_DB_PATH} not found."}))
        return

    conn = sqlite3.connect(str(ODDS_DB_PATH))
    print("Building closing line index...", file=sys.stderr)
    cl_index = _build_closing_index(conn)
    print(f"Index built: {len(cl_index)} entries", file=sys.stderr)
    conn.close()

    leans = []
    with open(LEAN_BETS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                leans.append(json.loads(line))

    total = len(leans)
    matched = 0
    enriched = []

    for lean in leans:
        stat_key = str(lean.get("stat") or "").lower()
        market = STAT_TO_MARKET.get(stat_key)
        if not market:
            enriched.append(lean)
            continue

        player_name = lean.get("playerName") or lean.get("player_name") or ""
        pick_date = lean.get("pickDate") or lean.get("pick_date") or lean.get("date") or ""
        line = _as_float(lean.get("line"))
        rec_side = str(lean.get("recommendedSide") or lean.get("recommended_side") or lean.get("side") or "").lower()
        rec_odds = lean.get("overOdds") or lean.get("over_odds") or lean.get("odds")
        if rec_side == "under":
            rec_odds = lean.get("underOdds") or lean.get("under_odds") or lean.get("odds")

        if not pick_date or not player_name:
            enriched.append(lean)
            continue

        cl = _lookup_closing(cl_index, market, player_name, pick_date)

        if cl:
            close_line = cl.get("close_line")
            close_over_odds = cl.get("close_over_odds")
            close_under_odds = cl.get("close_under_odds")
            close_rec_odds = close_over_odds if rec_side == "over" else close_under_odds

            lean["closingLine"] = close_line
            lean["closingOverOdds"] = close_over_odds
            lean["closingUnderOdds"] = close_under_odds
            lean["clvDelta"] = _clv_line_delta(rec_side, line, close_line)
            lean["clvOddsPct"] = _clv_odds_pct(rec_odds, close_rec_odds)
            matched += 1
        else:
            lean["closingLine"] = None
            lean["clvDelta"] = None
            lean["clvOddsPct"] = None

        enriched.append(lean)

    if dry_run:
        print(json.dumps({
            "success": True,
            "dryRun": True,
            "total": total,
            "matched": matched,
            "matchRate": round(matched / total * 100, 1) if total > 0 else 0,
        }))
        return

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        for lean in enriched:
            f.write(json.dumps(lean, separators=(",", ":")) + "\n")

    print(json.dumps({
        "success": True,
        "total": total,
        "matched": matched,
        "matchRate": round(matched / total * 100, 1) if total > 0 else 0,
        "outputPath": str(OUTPUT_PATH),
    }))


if __name__ == "__main__":
    main()
