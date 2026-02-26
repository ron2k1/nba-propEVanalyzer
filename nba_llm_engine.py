#!/usr/bin/env python3
"""LLM-powered reasoning layer using gpt-oss:20b (Ollama) with Claude fallback."""

import json
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv(override=True)

_OLLAMA_BASE = "http://localhost:11434"
_OLLAMA_MODEL = "gpt-oss:20b"
_CLAUDE_MODEL = "claude-sonnet-4-6"
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
    """Try Ollama (gpt-oss:20b) first, fall back to Claude."""
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

    articles_text = "\n".join([
        f"- [{s.get('source')}] {s.get('title')} (status: {s.get('status')}, conf: {s.get('confidence')})"
        for s in (news_signals or [])[:10]
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
    }


def llm_matchup_context(player_name, stat, projection, opponent_abbr, is_home,
                        opponent_defense=None, matchup_history=None):
    """
    Analyze matchup context and suggest a projection modifier.

    Returns dict with: modifier, reasoning, confidence, provider
    """
    defense_text = ""
    if opponent_defense:
        defense_text = (
            f"Opponent defense rank: {opponent_defense.get('rank')} | "
            f"Def rating: {opponent_defense.get('defRating')} | "
            f"Pts allowed to position: {opponent_defense.get('ptsAllowed')}"
        )

    history_text = ""
    if matchup_history:
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
        ev_text = (
            f"Model EV — Over: {over.get('evPercent')}% | Under: {under.get('evPercent')}%\n"
            f"Prob Over: {ev_data.get('probOver')} | Prob Under: {ev_data.get('probUnder')}\n"
            f"Distribution: {ev_data.get('distributionMode')}"
        )

    ref_text = ""
    if reference_book_meta:
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
    }


def llm_full_analysis(player_name, team_abbr, stat, line, projection,
                      opponent_abbr, is_home, ev_data=None, opponent_defense=None,
                      matchup_history=None, reference_book_meta=None, news_signals=None):
    """
    Run all 3 LLM analyses and return combined result.
    """
    injury = llm_injury_signal(player_name, team_abbr, news_signals or [])
    matchup = llm_matchup_context(player_name, stat, projection, opponent_abbr,
                                  is_home, opponent_defense, matchup_history)
    line_r = llm_line_reasoning(player_name, stat, line, projection,
                                ev_data, reference_book_meta)

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
