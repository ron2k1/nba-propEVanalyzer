#!/usr/bin/env python3
"""LLM-powered reasoning layer with configurable provider priority."""

import json
import os
import re

import requests
from dotenv import load_dotenv

from .nba_toon import to_toon_object, to_toon_table
from .nba_rag_client import query_rag

load_dotenv(override=True)

_OLLAMA_BASE = "http://localhost:11434"
_OLLAMA_MODEL = "gpt-oss:20b"
_CLAUDE_MODEL = "claude-sonnet-4-6"
_PROVIDER_ORDER = os.getenv("LLM_PROVIDER_ORDER", "ollama_first").strip().lower()
_PROMPT_FORMAT = os.getenv("LLM_PROMPT_FORMAT", "toon").strip().lower()
_TIMEOUT = 90


def _call_ollama(system_prompt, user_prompt):
    try:
        resp = requests.post(
            f"{_OLLAMA_BASE}/api/chat",
            json={
                "model": _OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return None, f"Ollama HTTP {resp.status_code}: {resp.text[:200]}"
        content = (resp.json().get("message") or {}).get("content", "")
        return content, None
    except requests.exceptions.ConnectionError:
        return None, "Ollama not running"
    except Exception as e:
        return None, str(e)


def _call_claude(system_prompt, user_prompt):
    try:
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return None, "ANTHROPIC_API_KEY not set"
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        content = message.content[0].text if message.content else ""
        return content, None
    except Exception as e:
        return None, str(e)


def _llm_call(system_prompt, user_prompt):
    """
    Call LLM providers in configured order.

    LLM_PROVIDER_ORDER:
      - ollama_first (default)
      - claude_first
    """
    if _PROVIDER_ORDER == "claude_first":
        content, err = _call_claude(system_prompt, user_prompt)
        if content:
            return content, "claude", None
        content, err2 = _call_ollama(system_prompt, user_prompt)
        if content:
            return content, "gpt-oss:20b", None
        return None, None, f"Claude: {err} | Ollama: {err2}"

    # Default behavior: Ollama first for low-cost runtime inference.
    content, err = _call_ollama(system_prompt, user_prompt)
    if content:
        return content, "gpt-oss:20b", None
    content, err2 = _call_claude(system_prompt, user_prompt)
    if content:
        return content, "claude", None
    return None, None, f"Ollama: {err} | Claude: {err2}"


def _parse_json(text):
    """Extract JSON from LLM response, handling markdown fences."""
    if not text:
        return None
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None


def llm_injury_signal(player_name, team_abbr, news_signals):
    """
    Interpret injury/news signals and return a projection adjustment %.

    Returns dict with: adjustmentPct, reasoning, confidence, provider
    """
    if not news_signals:
        return {"success": False, "error": "No news signals to analyze"}

    signals = (news_signals or [])[:10]
    if _PROMPT_FORMAT == "toon":
        articles_text = to_toon_table(
            [{"source": s.get("source"), "title": s.get("title"),
              "status": s.get("status"), "confidence": s.get("confidence")}
             for s in signals],
            columns=["source", "title", "status", "confidence"],
        )
    else:
        articles_text = "\n".join([
            f"- [{s.get('source')}] {s.get('title')} (status: {s.get('status')}, conf: {s.get('confidence')})"
            for s in signals
        ])

    system_prompt = (
        "You are an NBA prop betting analyst. Analyze injury/news signals and return a JSON "
        "projection adjustment for the target player. Be precise and data-driven."
    )
    user_prompt = (
        f"Player: {player_name} | Team: {team_abbr}\n\n"
        f"Recent injury/news signals:\n{articles_text}\n\n"
        "What percentage adjustment should be applied to this player's stat projections?\n"
        "Positive = boost (teammates out = more usage). Negative = penalty (player is hurt/limited).\n\n"
        "Return ONLY this JSON:\n"
        '{"adjustmentPct": <float -0.25 to 0.25>, "reasoning": "<1-2 sentences>", "confidence": <0.0 to 1.0>}'
    )

    rag_context = query_rag(f"injury patterns and projection adjustments for {player_name} {team_abbr}")
    if rag_context:
        user_prompt += f"\n\nHistorical context:\n{rag_context}"

    content, provider, err = _llm_call(system_prompt, user_prompt)
    if err:
        return {"success": False, "error": err}

    parsed = _parse_json(content)
    if not parsed:
        return {"success": False, "error": "Failed to parse LLM response", "raw": content}

    return {
        "success": True,
        "adjustmentPct": float(parsed.get("adjustmentPct") or 0.0),
        "reasoning": str(parsed.get("reasoning") or ""),
        "confidence": float(parsed.get("confidence") or 0.5),
        "provider": provider,
        "ragContext": bool(rag_context),
    }


def llm_matchup_context(player_name, stat, projection, opponent_abbr, is_home,
                        opponent_defense=None, matchup_history=None):
    """
    Analyze matchup context and suggest a projection modifier.

    Returns dict with: modifier, reasoning, confidence, provider
    """
    defense_text = ""
    if opponent_defense:
        if _PROMPT_FORMAT == "toon":
            defense_text = "Opponent defense:\n" + to_toon_object(opponent_defense)
        else:
            defense_text = (
                f"Opponent defense rank: {opponent_defense.get('rank')} | "
                f"Def rating: {opponent_defense.get('defRating')} | "
                f"Pts allowed to position: {opponent_defense.get('ptsAllowed')}"
            )

    history_text = ""
    if matchup_history:
        if _PROMPT_FORMAT == "toon":
            if isinstance(matchup_history, list) and matchup_history:
                history_text = f"Recent matchup history vs {opponent_abbr}:\n" + to_toon_table(matchup_history)
            else:
                history_text = f"Recent matchup history vs {opponent_abbr}: {json.dumps(matchup_history)}"
        else:
            history_text = f"Recent matchup history vs {opponent_abbr}: {json.dumps(matchup_history)}"

    system_prompt = (
        "You are an NBA prop betting analyst. Analyze matchup context and return a modifier "
        "for the player's projection. Focus on factors not captured in the base projection."
    )
    user_prompt = (
        f"Player: {player_name} | Stat: {stat.upper()} | Base projection: {projection}\n"
        f"Opponent: {opponent_abbr} | Home: {is_home}\n"
        f"{defense_text}\n{history_text}\n\n"
        "Suggest a multiplier modifier for this matchup (1.0 = no change).\n"
        "Consider: defensive scheme, pace of play, home/away edge, matchup trends.\n\n"
        "Return ONLY this JSON:\n"
        '{"modifier": <float 0.80 to 1.20>, "reasoning": "<1-2 sentences>", "confidence": <0.0 to 1.0>}'
    )

    rag_context = query_rag(f"matchup calibration findings for {stat} against {opponent_abbr}")
    if rag_context:
        user_prompt += f"\n\nHistorical context:\n{rag_context}"

    content, provider, err = _llm_call(system_prompt, user_prompt)
    if err:
        return {"success": False, "error": err}

    parsed = _parse_json(content)
    if not parsed:
        return {"success": False, "error": "Failed to parse LLM response", "raw": content}

    return {
        "success": True,
        "modifier": float(parsed.get("modifier") or 1.0),
        "reasoning": str(parsed.get("reasoning") or ""),
        "confidence": float(parsed.get("confidence") or 0.5),
        "provider": provider,
        "ragContext": bool(rag_context),
    }


def llm_line_reasoning(player_name, stat, line, projection, ev_data=None, reference_book_meta=None):
    """
    Assess whether a betting line looks sharp or soft.

    Returns dict with: verdict, sharpnessScore, reasoning, provider
    """
    ev_text = ""
    if ev_data:
        over = ev_data.get("over") or {}
        under = ev_data.get("under") or {}
        if _PROMPT_FORMAT == "toon":
            ev_text = "Model EV:\n" + to_toon_object({
                "evOver": f"{over.get('evPercent')}%",
                "evUnder": f"{under.get('evPercent')}%",
                "probOver": ev_data.get("probOver"),
                "probUnder": ev_data.get("probUnder"),
                "distribution": ev_data.get("distributionMode"),
            })
        else:
            ev_text = (
                f"Model EV — Over: {over.get('evPercent')}% | Under: {under.get('evPercent')}%\n"
                f"Prob Over: {ev_data.get('probOver')} | Prob Under: {ev_data.get('probUnder')}\n"
                f"Distribution: {ev_data.get('distributionMode')}"
            )

    ref_text = ""
    if reference_book_meta:
        if _PROMPT_FORMAT == "toon":
            ref_text = "Sharp book reference:\n" + to_toon_object({
                "book": reference_book_meta.get("book"),
                "noVigOver": reference_book_meta.get("noVigOver"),
                "noVigUnder": reference_book_meta.get("noVigUnder"),
            })
        else:
            ref_text = (
                f"Sharp book ({reference_book_meta.get('book')}): "
                f"No-vig Over {reference_book_meta.get('noVigOver')} | "
                f"No-vig Under {reference_book_meta.get('noVigUnder')}"
            )

    system_prompt = (
        "You are an NBA sharp betting analyst. Evaluate whether a prop line is sharp "
        "(efficient, hard to beat) or soft (beatable, clear value). Be direct."
    )
    user_prompt = (
        f"Player: {player_name} | Stat: {stat.upper()} | Line: {line} | Projection: {projection}\n"
        f"{ev_text}\n{ref_text}\n\n"
        "Assess the line sharpness and betting value.\n\n"
        "Return ONLY this JSON:\n"
        '{"verdict": "sharp"|"soft"|"neutral", "sharpnessScore": <int 1-10>, "reasoning": "<2-3 sentences>"}'
    )

    rag_context = query_rag(f"calibration lessons for {stat} lines and edge thresholds")
    if rag_context:
        user_prompt += f"\n\nHistorical context:\n{rag_context}"

    content, provider, err = _llm_call(system_prompt, user_prompt)
    if err:
        return {"success": False, "error": err}

    parsed = _parse_json(content)
    if not parsed:
        return {"success": False, "error": "Failed to parse LLM response", "raw": content}

    return {
        "success": True,
        "verdict": str(parsed.get("verdict") or "neutral"),
        "sharpnessScore": int(parsed.get("sharpnessScore") or 5),
        "reasoning": str(parsed.get("reasoning") or ""),
        "provider": provider,
        "ragContext": bool(rag_context),
    }


def _heuristic_matchup_context(opponent_abbr, is_home):
    venue = "home" if is_home else "road"
    return {
        "success": True,
        "modifier": 1.0,
        "reasoning": (
            f"No structured defense or matchup-history data was available for {opponent_abbr}; "
            f"keeping a neutral matchup modifier (1.00) on the {venue} split."
        ),
        "confidence": 0.5,
        "provider": "heuristic",
    }


def _heuristic_line_reasoning(stat, line, projection, ev_data=None):
    over = (ev_data or {}).get("over") or {}
    under = (ev_data or {}).get("under") or {}
    over_ev = float(over.get("evPercent") or 0.0)
    under_ev = float(under.get("evPercent") or 0.0)
    best_side = "over" if over_ev >= under_ev else "under"
    best_ev = over_ev if best_side == "over" else under_ev
    best_edge = float(((over if best_side == "over" else under).get("edge")) or 0.0)
    best_prob = float((ev_data or {}).get("probOver") or 0.5) if best_side == "over" else float((ev_data or {}).get("probUnder") or 0.5)

    if best_ev >= 8.0 and best_edge >= 0.05:
        verdict = "soft"
        score = 3
    elif best_ev >= 3.0 and best_edge >= 0.02:
        verdict = "neutral"
        score = 5
    else:
        verdict = "sharp"
        score = 8

    return {
        "success": True,
        "verdict": verdict,
        "sharpnessScore": score,
        "reasoning": (
            f"Model projection ({projection}) vs line ({line}) on {stat.upper()} implies a best-side "
            f"{best_side} edge of {best_edge:.3f} ({best_ev:.2f}% EV, {best_prob:.1%} win probability), "
            f"so the line grades as {verdict} in this fast heuristic mode."
        ),
        "provider": "heuristic",
    }


def llm_full_analysis(player_name, team_abbr, stat, line, projection,
                      opponent_abbr, is_home, ev_data=None, opponent_defense=None,
                      matchup_history=None, reference_book_meta=None, news_signals=None):
    """
    Run all 3 LLM analyses and return combined result.
    """
    injury = llm_injury_signal(player_name, team_abbr, news_signals or [])

    has_structured_matchup = bool(opponent_defense) or bool(matchup_history)
    if has_structured_matchup:
        matchup = llm_matchup_context(player_name, stat, projection, opponent_abbr,
                                      is_home, opponent_defense, matchup_history)
    else:
        matchup = _heuristic_matchup_context(opponent_abbr, is_home)

    if reference_book_meta:
        line_r = llm_line_reasoning(player_name, stat, line, projection, ev_data, reference_book_meta)
    else:
        line_r = _heuristic_line_reasoning(stat, line, projection, ev_data)

    return {
        "success": True,
        "player": player_name,
        "stat": stat,
        "line": line,
        "projection": projection,
        "injurySignal": injury,
        "matchupContext": matchup,
        "lineReasoning": line_r,
    }
