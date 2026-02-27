#!/usr/bin/env python3
"""
LocalNBAStats — serves historical NBA data from a pre-built pickle index
(built by scripts/index_local_data.py from the Kaggle nathanlauga/nba-games dataset)
without making any API calls.

Drop-in replacement for the relevant functions in nba_data_collection.py:
    get_player_game_log
    get_player_splits
    get_team_defensive_ratings

Used by nba_backtest.py when data_source="local".
"""

import math
import os
import pickle
import statistics
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_INDEX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "reference", "kaggle_nba", "index.pkl",
)


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_round(v, n=1):
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return None


def _safe_div(a, b):
    return a / b if b else 0.0


def _parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(s)[:10], fmt).date()
        except ValueError:
            continue
    return None


def _compute_rolling(logs):
    """Matches nba_data_collection rolling dict shape."""
    keys = ["pts", "reb", "ast", "stl", "blk", "tov", "fg3m",
            "pra", "pr", "pa", "ra", "min"]
    rolling = {}
    for key in keys:
        vals = [_safe_float(g.get(key, 0)) for g in logs]
        n = len(vals)
        rolling[f"{key}_avg5"]       = _safe_round(statistics.mean(vals[:5]),  1) if n >= 5  else (_safe_round(statistics.mean(vals), 1) if vals else 0)
        rolling[f"{key}_avg10"]      = _safe_round(statistics.mean(vals[:10]), 1) if n >= 10 else (_safe_round(statistics.mean(vals), 1) if vals else 0)
        rolling[f"{key}_avg_season"] = _safe_round(statistics.mean(vals),      1) if vals else 0
        rolling[f"{key}_median"]     = _safe_round(statistics.median(vals),    1) if vals else 0
        rolling[f"{key}_stdev"]      = _safe_round(statistics.stdev(vals),     2) if n >= 2  else 0
        rolling[f"{key}_min"]        = min(vals) if vals else 0
        rolling[f"{key}_max"]        = max(vals) if vals else 0
    return rolling


def _compute_hit_rates(logs):
    """Matches nba_data_collection hit_rates dict shape."""
    hit_rates = {}
    for key in ["pts", "reb", "ast", "fg3m", "pra", "stl", "blk", "tov"]:
        vals = [_safe_float(g.get(key, 0)) for g in logs]
        if not vals:
            continue
        avg = statistics.mean(vals)
        primary_line = math.floor(avg) + 0.5
        alt_lines = {}
        for offset in [-3.0, -1.5, 0.0, 1.5, 3.0]:
            cl = max(0.5, primary_line + offset)
            over_ct = sum(1 for v in vals if v > cl)
            alt_lines[str(cl)] = _safe_round(_safe_div(over_ct, len(vals)) * 100, 1)
        over_primary  = sum(1 for v in vals if v > primary_line)
        under_primary = len(vals) - over_primary
        hit_rates[key] = {
            "line":       primary_line,
            "overRate":   _safe_round(_safe_div(over_primary,  len(vals)) * 100, 1),
            "underRate":  _safe_round(_safe_div(under_primary, len(vals)) * 100, 1),
            "sampleSize": len(vals),
            "avg":        _safe_round(avg, 1),
            "altLines":   alt_lines,
        }
    return hit_rates


def _extract_split_stats(logs):
    """Compute per-game averages over a set of game logs."""
    if not logs:
        return None
    n = len(logs)
    def avg(key):
        return _safe_round(statistics.mean(_safe_float(g.get(key, 0)) for g in logs), 1)
    pts = avg("pts")
    reb = avg("reb")
    ast = avg("ast")
    return {
        "gp":    n,
        "min":   avg("min"),
        "pts":   pts,
        "reb":   reb,
        "ast":   ast,
        "stl":   avg("stl"),
        "blk":   avg("blk"),
        "tov":   avg("tov"),
        "fg3m":  avg("fg3m"),
        "fgPct": avg("fgPct"),
        "ftPct": avg("ftPct"),
        "pra":   _safe_round(
            statistics.mean(
                (_safe_float(g.get("pts", 0)) + _safe_float(g.get("reb", 0)) + _safe_float(g.get("ast", 0)))
                for g in logs
            ), 1
        ),
    }


# ---------------------------------------------------------------------------
# LocalNBAStats
# ---------------------------------------------------------------------------

class LocalNBAStats:
    """
    Loads the pickle index once on init, then serves data without API calls.

    Call signatures intentionally match nba_data_collection equivalents so the
    backtest can monkey-patch dc.get_player_game_log etc.
    """

    def __init__(self, index_path=None):
        path = index_path or _DEFAULT_INDEX
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Local NBA index not found at {path}. "
                "Run: .venv/Scripts/python.exe scripts/index_local_data.py"
            )
        with open(path, "rb") as fh:
            self._index = pickle.load(fh)

        self._games_by_date    = self._index.get("games_by_date", {})
        self._boxscore_by_game = self._index.get("boxscore_by_game", {})
        self._gamelogs         = self._index.get("gamelogs_by_player", {})
        self._team_abbr        = self._index.get("team_id_to_abbr", {})
        self._player_positions = self._index.get("player_positions", {})
        self.schema            = self._index.get("schema", "unknown")
        self.seasons_covered   = self._index.get("seasons_covered", [])
        self.min_date          = self._index.get("min_date", "")
        self.max_date          = self._index.get("max_date", "2023-06-01")
        self._defense_cache    = {}  # keyed by as_of_date string; avoids re-scanning per projection

    # ------------------------------------------------------------------
    # Public API (match nba_data_collection signatures)
    # ------------------------------------------------------------------

    def get_player_game_log(self, player_id, season=None, last_n=25, as_of_date=None):
        """
        Returns dict matching nba_data_collection.get_player_game_log output:
        {"success": True, "gameLogs": [...], "rolling": {...}, "hitRates": {...},
         "playerId": ..., "gamesPlayed": N, "gamesExcludedDnp": N}
        """
        try:
            all_logs = self._gamelogs.get(int(player_id), [])
            cutoff   = _parse_date(as_of_date)

            # Filter to games strictly before as_of_date
            if cutoff:
                logs = [g for g in all_logs if g.get("_date_str", "") < cutoff.isoformat()]
            else:
                logs = list(all_logs)

            # Filter by season if provided
            if season:
                logs = [g for g in logs if g.get("season") == season]

            # Most recent first (logs stored ascending, we reverse for last_n)
            logs_recent_first = list(reversed(logs))

            # DNP threshold (match nba_data_collection behaviour: min >= 1)
            games_before = len(logs_recent_first)
            logs_recent_first = [g for g in logs_recent_first if _safe_float(g.get("min", 0)) >= 1]
            games_excluded_dnp = games_before - len(logs_recent_first)

            if last_n is not None:
                logs_recent_first = logs_recent_first[:max(1, int(last_n))]

            rolling   = _compute_rolling(logs_recent_first)
            hit_rates = _compute_hit_rates(logs_recent_first)

            return {
                "success":         True,
                "gameLogs":        logs_recent_first,
                "rolling":         rolling,
                "hitRates":        hit_rates,
                "playerId":        player_id,
                "gamesPlayed":     len(logs_recent_first),
                "gamesExcludedDnp": games_excluded_dnp,
            }
        except Exception as e:
            return {
                "success": False, "error": str(e),
                "gameLogs": [], "rolling": {}, "hitRates": {},
                "playerId": player_id, "gamesPlayed": 0, "gamesExcludedDnp": 0,
            }

    def get_player_splits(self, player_id, season=None, as_of_date=None):
        """
        Returns dict matching nba_data_collection.get_player_splits output:
        {"success": True, "splits": {"overall", "home", "away", "restDays", "wins", "losses"}, "playerId": ...}
        """
        try:
            all_logs = self._gamelogs.get(int(player_id), [])
            cutoff   = _parse_date(as_of_date)

            if cutoff:
                logs = [g for g in all_logs if g.get("_date_str", "") < cutoff.isoformat()]
            else:
                logs = list(all_logs)

            if season:
                logs = [g for g in logs if g.get("season") == season]

            # Filter DNPs
            logs = [g for g in logs if _safe_float(g.get("min", 0)) >= 1]

            home_logs = [g for g in logs if g.get("isHome")]
            away_logs = [g for g in logs if not g.get("isHome")]
            win_logs  = [g for g in logs if g.get("wl") == "W"]
            loss_logs = [g for g in logs if g.get("wl") == "L"]

            # Rest days: derive from consecutive game dates
            rest_days = {}
            date_strs = sorted(g["_date_str"] for g in logs if "_date_str" in g)
            if date_strs:
                prev = None
                rest_bucket = {"0": [], "1": [], "2+": []}
                log_by_date = {g["_date_str"]: g for g in logs}
                for ds in date_strs:
                    g = log_by_date.get(ds)
                    if not g:
                        continue
                    if prev is None:
                        bucket = "2+"
                    else:
                        try:
                            d_cur  = datetime.strptime(ds,   "%Y-%m-%d").date()
                            d_prev = datetime.strptime(prev, "%Y-%m-%d").date()
                            delta  = (d_cur - d_prev).days - 1
                            if delta <= 0:
                                bucket = "0"
                            elif delta == 1:
                                bucket = "1"
                            else:
                                bucket = "2+"
                        except ValueError:
                            bucket = "2+"
                    rest_bucket[bucket].append(g)
                    prev = ds
                for key, bucket_logs in rest_bucket.items():
                    sp = _extract_split_stats(bucket_logs)
                    if sp:
                        rest_days[key] = sp

            result = {
                "overall":  _extract_split_stats(logs),
                "home":     _extract_split_stats(home_logs),
                "away":     _extract_split_stats(away_logs),
                "restDays": rest_days,
                "wins":     _extract_split_stats(win_logs),
                "losses":   _extract_split_stats(loss_logs),
            }

            return {"success": True, "splits": result, "playerId": player_id}
        except Exception as e:
            return {"success": False, "error": str(e), "splits": None, "playerId": player_id}

    def get_team_defensive_ratings(self, as_of_date=None):
        """
        Compute defensive ratings from boxscore data (stats allowed per game).

        Returns dict matching nba_data_collection.get_team_defensive_ratings:
        {"success": True, "teams": [...], "leagueAvg": {...}}
        """
        cache_key = str(as_of_date)[:10] if as_of_date else "full"
        if cache_key in self._defense_cache:
            return self._defense_cache[cache_key]

        try:
            cutoff = _parse_date(as_of_date)

            # We want ~82 game sample before cutoff — use season context
            # Collect per-team opponent stats from the boxscore
            # Strategy: for each game, the stats scored by team A = stats *allowed* by team B
            team_allowed = {}  # team_id → list of game stat dicts (what was scored *against* them)

            # Build a map from game date for filtering
            for date_str, day_games in self._games_by_date.items():
                if cutoff and date_str >= cutoff.isoformat():
                    continue

                for gmeta in day_games:
                    game_id  = gmeta["gameId"]
                    home_id  = gmeta["homeTeamId"]
                    away_id  = gmeta["awayTeamId"]

                    rows = self._boxscore_by_game.get(game_id, [])
                    if not rows:
                        continue

                    # Aggregate home and away team totals
                    home_totals = {"pts": 0, "reb": 0, "ast": 0, "stl": 0, "blk": 0, "tov": 0, "fg3m": 0}
                    away_totals = {"pts": 0, "reb": 0, "ast": 0, "stl": 0, "blk": 0, "tov": 0, "fg3m": 0}

                    for r in rows:
                        tid = int(r.get("TEAM_ID", 0) or 0)
                        if tid == home_id:
                            target = home_totals
                        elif tid == away_id:
                            target = away_totals
                        else:
                            continue
                        target["pts"]  += int(r.get("PTS",  0) or 0)
                        target["reb"]  += int(r.get("REB",  0) or 0)
                        target["ast"]  += int(r.get("AST",  0) or 0)
                        target["stl"]  += int(r.get("STL",  0) or 0)
                        target["blk"]  += int(r.get("BLK",  0) or 0)
                        target["tov"]  += int(r.get("TOV",  0) or 0)
                        target["fg3m"] += int(r.get("FG3M", 0) or 0)

                    # Home allowed = away totals; away allowed = home totals
                    for team_id, allowed in [(home_id, away_totals), (away_id, home_totals)]:
                        if team_id not in team_allowed:
                            team_allowed[team_id] = []
                        team_allowed[team_id].append(allowed)

            # Compute per-team averages of stats allowed
            stat_keys = ["pts", "reb", "ast", "stl", "blk", "tov", "fg3m"]
            opp_key_map = {
                "pts": "OPP_PTS", "reb": "OPP_REB", "ast": "OPP_AST",
                "stl": "OPP_STL", "blk": "OPP_BLK", "tov": "OPP_TOV", "fg3m": "OPP_FG3M",
            }

            team_avgs = {}
            for team_id, game_list in team_allowed.items():
                if not game_list:
                    continue
                avgs = {}
                for sk in stat_keys:
                    avgs[sk] = statistics.mean(g[sk] for g in game_list)
                team_avgs[team_id] = {"gp": len(game_list), "avgs": avgs}

            # League averages
            if not team_avgs:
                return {"success": True, "teams": [], "leagueAvg": {}}

            league_avg = {}
            for sk in stat_keys:
                vals = [team_avgs[tid]["avgs"][sk] for tid in team_avgs]
                league_avg[opp_key_map[sk]] = statistics.mean(vals) if vals else 0.0

            def def_mult(sk, val):
                la_key = opp_key_map[sk]
                la = league_avg.get(la_key, 0)
                return _safe_round(val / la, 3) if la > 0 else 1.0

            teams_result = []
            for team_id, data in team_avgs.items():
                avgs = data["avgs"]
                abbr = self._team_abbr.get(team_id, "")
                teams_result.append({
                    "teamId":         team_id,
                    "abbreviation":   abbr,
                    "name":           abbr,
                    "gp":             data["gp"],
                    "wins":           0,
                    "losses":         0,
                    "winPct":         0.0,
                    "defPtsAllowed":  _safe_round(avgs["pts"]),
                    "defRebAllowed":  _safe_round(avgs["reb"]),
                    "defAstAllowed":  _safe_round(avgs["ast"]),
                    "defStlAllowed":  _safe_round(avgs["stl"]),
                    "defBlkAllowed":  _safe_round(avgs["blk"]),
                    "defTovForced":   _safe_round(avgs["tov"]),
                    "defFg3mAllowed": _safe_round(avgs["fg3m"]),
                    "defPtsMult":     def_mult("pts",  avgs["pts"]),
                    "defRebMult":     def_mult("reb",  avgs["reb"]),
                    "defAstMult":     def_mult("ast",  avgs["ast"]),
                    "defStlMult":     def_mult("stl",  avgs["stl"]),
                    "defBlkMult":     def_mult("blk",  avgs["blk"]),
                    "defTovMult":     def_mult("tov",  avgs["tov"]),
                    "defFg3mMult":    def_mult("fg3m", avgs["fg3m"]),
                    # Advanced fields not available from Kaggle — default to neutral
                    "pace":      100,
                    "paceFactor": 1.0,
                    "offRtg":    110,
                    "defRtg":    110,
                    "netRtg":    0,
                    "defPtsRank":  15,
                    "defRebRank":  15,
                    "defAstRank":  15,
                    "defFg3mRank": 15,
                })

            result = {"success": True, "teams": teams_result, "leagueAvg": league_avg}
            self._defense_cache[cache_key] = result
            return result
        except Exception as e:
            return {"success": False, "error": str(e), "teams": [], "leagueAvg": {}}

    def get_games_for_date(self, date_str):
        """
        Returns list of game dicts for a given YYYY-MM-DD date string.
        Shape: [{"gameId", "homeTeamId", "awayTeamId", "homeAbbr", "awayAbbr"}]
        """
        return self._games_by_date.get(str(date_str)[:10], [])

    def get_boxscore_players(self, game_id):
        """
        Returns list of player rows for a given game_id.
        Shape: [{"PLAYER_ID", "TEAM_ID", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV", "FG3M"}]
        """
        # Try with and without zero-padding
        rows = self._boxscore_by_game.get(str(game_id), [])
        if not rows:
            # Try zero-padded form
            rows = self._boxscore_by_game.get(str(game_id).zfill(10), [])
        return rows

    def get_player_position(self, player_id):
        """
        Local replacement for nba_data_collection.get_player_position.
        """
        try:
            pid = int(player_id)
        except (TypeError, ValueError):
            return {"success": False, "error": f"invalid player_id: {player_id}"}

        pos = str(self._player_positions.get(pid, "G") or "G").upper().strip()
        if pos not in {"G", "F", "C"}:
            pos = "G"
        return {
            "success": True,
            "playerId": pid,
            "position": pos,
            "source": "local_index",
        }

    def get_position_vs_team(self, team_id, season=None, as_of_date=None):
        """
        Local replacement for nba_data_collection.get_position_vs_team.
        We return a neutral response so projection math remains stable
        without forcing external API calls.
        """
        _ = (team_id, season, as_of_date)
        return {
            "success": False,
            "error": "position-vs-team split unavailable in local index",
            "multipliers": None,
        }
