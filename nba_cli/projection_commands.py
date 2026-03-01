#!/usr/bin/env python3
"""Projection and live projection CLI commands."""

import math
import sys
from datetime import datetime

from core.nba_data_collection import get_live_player_stats, safe_round
from core.nba_data_prep import compute_projection, compute_usage_adjustment
from core.nba_model_training import compute_live_projection
from core.nba_starter_accuracy import run_starter_accuracy

from .shared import VALID_LIVE_PROJECTION_STATS, resolve_player_or_result

_STAT_WORDS = {
    "pts": ("Points", "points"),
    "reb": ("Rebounds", "rebounds"),
    "ast": ("Assists", "assists"),
    "stl": ("Steals", "steals"),
    "blk": ("Blocks", "blocks"),
    "tov": ("Turnovers", "turnovers"),
    "fg3m": ("3PM", "threes"),
    "pra": ("PRA", "pra"),
}


def _format_num(value, decimals=1):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.{decimals}f}"


def _period_label(period):
    try:
        p = int(period)
    except (TypeError, ValueError):
        return "In-game"
    if p <= 0:
        return "Pre-game"
    if 1 <= p <= 4:
        return f"Q{p}"
    if p == 5:
        return "OT"
    return f"{p - 4}OT"


def _render_metric_box(rows):
    encoding = (getattr(sys.stdout, "encoding", None) or "utf-8").lower()
    use_unicode = True
    try:
        "┌".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        use_unicode = False

    if use_unicode:
        chars = {
            "tl": "┌", "tm": "┬", "tr": "┐",
            "ml": "├", "mm": "┼", "mr": "┤",
            "bl": "└", "bm": "┴", "br": "┘",
            "h": "─", "v": "│",
        }
    else:
        chars = {
            "tl": "+", "tm": "+", "tr": "+",
            "ml": "+", "mm": "+", "mr": "+",
            "bl": "+", "bm": "+", "br": "+",
            "h": "-", "v": "|",
        }

    left_w = max(len("Metric"), *(len(m) for m, _ in rows))
    right_w = max(len("Value"), *(len(v) for _, v in rows))

    def line(left, mid, right, fill):
        return f"{left}{fill * (left_w + 2)}{mid}{fill * (right_w + 2)}{right}"

    out = [
        line(chars["tl"], chars["tm"], chars["tr"], chars["h"]),
        f"{chars['v']} {'Metric'.ljust(left_w)} {chars['v']} {'Value'.ljust(right_w)} {chars['v']}",
        line(chars["ml"], chars["mm"], chars["mr"], chars["h"]),
    ]
    for i, (metric, value) in enumerate(rows):
        out.append(f"{chars['v']} {metric.ljust(left_w)} {chars['v']} {value.ljust(right_w)} {chars['v']}")
        if i < len(rows) - 1:
            out.append(line(chars["ml"], chars["mm"], chars["mr"], chars["h"]))
    out.append(line(chars["bl"], chars["bm"], chars["br"], chars["h"]))
    return "\n".join(out)


def _print_live_projection_pretty(payload, stat_key, line_value=None):
    label, noun = _STAT_WORDS.get(stat_key, (stat_key.upper(), stat_key))
    current = float(payload.get("currentStat") or 0.0)
    mins_played = float(payload.get("minsPlayed") or 0.0)
    proj_mins = float(payload.get("projectedMinutes") or 0.0)
    pace_pct = float(payload.get("gamePacePct") or 0.0)
    remaining = float(payload.get("remainingMins") or 0.0)
    per_min = float(payload.get("perMinRate") or 0.0)
    live_proj = float(payload.get("liveProjection") or 0.0)
    pregame = float(payload.get("pregameProjection") or 0.0)
    projected_remaining = live_proj - current
    pct_remaining = max(0.0, 100.0 - pace_pct)
    blend_weight = float(payload.get("blendWeight") or 0.0)

    rows = [
        (f"Current {label}", _format_num(current, 1)),
        ("Minutes Played", f"{_format_num(mins_played, 1)} of {_format_num(proj_mins, 1)} projected"),
        ("Game Progress", f"{_format_num(pace_pct, 1)}% through his mins"),
        ("Per-Min Rate", f"{_format_num(per_min, 3)} {stat_key}/min"),
        ("Live Blend Weight", f"{_format_num(blend_weight * 100.0, 1)}%"),
        ("Remaining Mins", _format_num(remaining, 1)),
        ("Projected Remaining", f"{'+' if projected_remaining >= 0 else ''}{_format_num(projected_remaining, 1)} {noun}"),
        ("Live Projection", _format_num(live_proj, 1)),
        ("Pregame Projection", _format_num(pregame, 1)),
    ]
    if line_value is not None:
        rows.append(("Line", _format_num(line_value, 1)))
    prob_over = payload.get("lineProbOver")
    if prob_over is not None:
        rows.append(("Chance Over Line", f"{_format_num(float(prob_over) * 100.0, 1)}%"))

    print(_render_metric_box(rows))

    period = _period_label(payload.get("period"))
    tempo_blurb = (
        "Still very early"
        if pace_pct < 30
        else ("Mid-game" if pace_pct < 70 else "Late-game")
    )

    if line_value is None:
        print(
            f"\nModel projects {_format_num(live_proj, 1)} {noun} with "
            f"{_format_num(pct_remaining, 1)}% of projected minutes remaining. "
            f"{tempo_blurb} ({period}, {_format_num(pace_pct, 1)}% through). "
            f"Live blend is {_format_num(blend_weight * 100.0, 1)}%."
        )
        return

    edge = live_proj - float(line_value)
    if abs(edge) < 1e-9:
        side_text = f"right on the {_format_num(line_value, 1)} line"
    elif edge > 0:
        side_text = f"{_format_num(abs(edge), 1)} over the line"
    else:
        side_text = f"{_format_num(abs(edge), 1)} under the line"

    line_prob_msg = ""
    if prob_over is not None:
        line_prob_msg = f" Chance over line: {_format_num(float(prob_over) * 100.0, 1)}%."

    print(
        f"\nModel projects {_format_num(live_proj, 1)} {noun}, {side_text}, with "
        f"{_format_num(pct_remaining, 1)}% of projected minutes remaining. "
        f"{tempo_blurb} ({period}, {_format_num(pace_pct, 1)}% through). "
        f"Live blend: {_format_num(blend_weight * 100.0, 1)}%.{line_prob_msg}"
    )


def _normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _compute_live_line_probs(live_projection, line_value, stdev_full_game, remaining_mins, projected_mins):
    try:
        mu = float(live_projection)
        line = float(line_value)
        sd_full = float(stdev_full_game)
        rem = float(remaining_mins)
        total = float(projected_mins)
    except (TypeError, ValueError):
        return None

    if sd_full <= 0:
        return None

    rem_ratio = rem / total if total > 0 else 1.0
    rem_ratio = max(0.02, min(1.0, rem_ratio))
    # Conditional variance shrinks as remaining minutes shrink.
    live_sd = max(0.35, sd_full * math.sqrt(rem_ratio))
    z = (line - mu) / live_sd
    prob_over = 1.0 - _normal_cdf(z)
    prob_over = max(0.0, min(1.0, prob_over))

    return {
        "lineForProbability": line,
        "liveStdev": safe_round(live_sd, 3),
        "lineProbOver": safe_round(prob_over, 4),
        "lineProbUnder": safe_round(1.0 - prob_over, 4),
        "lineProbMode": "live_conditional_normal",
    }


def _handle_projection(argv):
    if len(argv) < 5:
        return {"error": "Usage: projection <player_id_or_name> <opponent_abbr> <is_home:0|1> [is_b2b:0|1]"}
    player_id, err = resolve_player_or_result(argv[2])
    if err:
        return err
    opponent = argv[3]
    is_home = argv[4] == "1"
    is_b2b = (argv[5] == "1") if len(argv) > 5 else False
    result = compute_projection(player_id, opponent, is_home, is_b2b)
    if result.get("success"):
        result["resolvedPlayerId"] = player_id
    return result


def _handle_live_projection(argv):
    if len(argv) < 7:
        return {
            "error": (
                "Usage: live_projection <player_id_or_name> <team_abbr> "
                "<opponent_abbr> <is_home:0|1> <stat> [line] [--pretty]"
            )
        }
    player_id, err = resolve_player_or_result(argv[2])
    if err:
        return err

    team_abbr = argv[3].upper()
    opp_abbr = argv[4].upper()
    is_home = argv[5] == "1"
    stat_key = argv[6].lower()
    extras = argv[7:]

    pretty_mode = any(str(x).strip().lower() in {"--pretty", "-p"} for x in extras)
    line_value = None
    for token in extras:
        t = str(token).strip()
        if t.lower() in {"--pretty", "-p"}:
            continue
        if line_value is None:
            try:
                line_value = float(t)
            except ValueError:
                continue

    if stat_key not in VALID_LIVE_PROJECTION_STATS:
        return {
            "success": False,
            "error": f"Unsupported stat '{stat_key}' for live projection.",
            "validStats": sorted(VALID_LIVE_PROJECTION_STATS),
        }

    live_data = get_live_player_stats(player_id, team_abbr)
    if not live_data.get("success"):
        return live_data

    pregame = compute_projection(player_id, opp_abbr, is_home, False)
    if not pregame.get("success"):
        return pregame

    projections = pregame.get("projections", {}) or {}
    stat_proj = dict(projections.get(stat_key) or {})
    if not stat_proj:
        return {
            "success": False,
            "error": f"Unsupported stat '{stat_key}' for live projection.",
            "validStats": sorted(projections.keys()),
        }

    usage_data = compute_usage_adjustment(player_id, team_abbr)
    if usage_data.get("success") and usage_data.get("absentTeammates"):
        stat_mult = float((usage_data.get("statMultipliers") or {}).get(stat_key, 1.0) or 1.0)
        if stat_mult > 0 and abs(stat_mult - 1.0) > 1e-9:
            for k in ("projection", "projectionModel", "projectionPreBlend"):
                if stat_proj.get(k) is not None:
                    stat_proj[k] = safe_round(float(stat_proj[k]) * stat_mult, 1)
            if stat_proj.get("perMinRate") is not None:
                stat_proj["perMinRate"] = safe_round(float(stat_proj.get("perMinRate")) * stat_mult, 4)
            stat_proj["usageMultiplier"] = safe_round(stat_mult, 3)
    else:
        usage_data = None

    live_inputs = dict(live_data.get("stats") or {})
    live_inputs["period"] = live_data.get("period")
    live_inputs["scoreMargin"] = live_data.get("scoreMargin")
    live_proj = compute_live_projection(stat_proj, live_inputs, stat_key)
    result = {
        **live_proj,
        "gameId": live_data.get("gameId"),
        "period": live_data.get("period"),
        "gameStatus": live_data.get("gameStatus"),
        "teamScore": live_data.get("teamScore"),
        "oppScore": live_data.get("oppScore"),
        "scoreMargin": live_data.get("scoreMargin"),
        "liveStats": live_data.get("stats"),
        "usageAdjustment": usage_data,
        "playerId": player_id,
        "playerTeam": team_abbr,
        "opponent": opp_abbr,
        "isHome": is_home,
    }
    if line_value is not None:
        result["line"] = line_value
        line_probs = _compute_live_line_probs(
            live_projection=result.get("liveProjection"),
            line_value=line_value,
            stdev_full_game=stat_proj.get("projStdev") or stat_proj.get("stdev"),
            remaining_mins=result.get("remainingMins"),
            projected_mins=result.get("projectedMinutes"),
        )
        if line_probs:
            result.update(line_probs)
    if pretty_mode and result.get("success"):
        _print_live_projection_pretty(result, stat_key, line_value=line_value)
    return result


def _handle_starter_accuracy(argv):
    # starter_accuracy [date_yyyy-mm-dd] [bookmakers_csv] [regions] [sport] [model_variant]
    rest = list(argv[2:])
    date_str = None
    if rest:
        maybe_date = str(rest[0]).strip()
        try:
            datetime.strptime(maybe_date, "%Y-%m-%d")
            date_str = maybe_date
            rest = rest[1:]
        except ValueError:
            pass

    bookmakers = rest[0] if len(rest) > 0 else "betmgm,draftkings,fanduel"
    regions = rest[1] if len(rest) > 1 else "us"
    sport = rest[2] if len(rest) > 2 else "basketball_nba"
    model_variant = rest[3] if len(rest) > 3 else "full"
    return run_starter_accuracy(
        date_str=date_str,
        bookmakers=bookmakers,
        regions=regions,
        sport=sport,
        model_variant=model_variant,
    )


_COMMANDS = {
    "projection":       _handle_projection,
    "live_projection":  _handle_live_projection,
    "starter_accuracy": _handle_starter_accuracy,
}


def handle_projection_command(command, argv):  # shim — router no longer calls this
    h = _COMMANDS.get(command)
    return h(argv) if h else None
