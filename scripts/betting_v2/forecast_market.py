#!/usr/bin/env python3
"""Prematch-only exact market probability mapping shared by research and V2 inference.

This module MUST NOT inspect observed outcomes, homeGoals/awayGoals, final
scores or post-kickoff stats. Settlement belongs in evaluate_holdout_prices.py.
No API calls, no quote certification and no STRONG/production publishing.
"""
from __future__ import annotations

from math import isfinite
from walk_forward_goals import GOAL_LINES


def _prob(probs: dict, key: str) -> float:
    value = probs[key]
    if isinstance(value, bool):
        raise ValueError("Boolean market probability")
    parsed = float(value)
    if not isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError("Nonfinite or invalid market probability")
    return parsed


def market_probability(quote: dict, forecast: dict) -> tuple[str, float, float] | None:
    """Return (market identity, P(win), P(push)) using PREMATCH forecast only.

    Unsupported or non-binary markets return None; malformed forecast
    probabilities raise ValueError. Asian lines and handicaps are excluded.
    """
    probs = forecast["probabilities"]
    market = str(quote.get("market") or "")
    direction = str(quote.get("direction") or "").upper()
    team = str(quote.get("teamSide") or "").upper()
    label = ""
    p = push = 0.0

    if market == "RESULT_1X2" and direction in ("HOME", "DRAW", "AWAY"):
        label = f"1X2_{direction}"
        p = _prob(probs, label)
    elif market == "RESULT_DNB" and direction in ("HOME", "AWAY"):
        label = f"1X2_{direction}"
        p = _prob(probs, label)
        push = _prob(probs, "1X2_DRAW")
    elif market == "RESULT_DOUBLE_CHANCE" and direction in (
        "HOME_OR_DRAW", "AWAY_OR_DRAW", "HOME_OR_AWAY"
    ):
        components = {
            "HOME_OR_DRAW": ("1X2_HOME", "1X2_DRAW"),
            "AWAY_OR_DRAW": ("1X2_AWAY", "1X2_DRAW"),
            "HOME_OR_AWAY": ("1X2_HOME", "1X2_AWAY"),
        }[direction]
        label = "DOUBLE_CHANCE_" + direction
        p = sum(_prob(probs, c) for c in components)
    elif market in ("FULL_TIME_MATCH_TOTAL", "HOME_TEAM_TOTAL", "AWAY_TEAM_TOTAL") and (
        direction in ("OVER", "UNDER")
    ):
        raw_line = quote.get("line")
        if isinstance(raw_line, bool):
            return None
        try:
            numeric_line = float(raw_line)
        except (TypeError, ValueError):
            return None
        if numeric_line not in GOAL_LINES:
            return None
        if market == "FULL_TIME_MATCH_TOTAL":
            if team not in ("", "MATCH", "NONE", "BOTH"):
                return None
            prefix = "MATCH"
        elif market == "HOME_TEAM_TOTAL":
            if team not in ("", "HOME"):
                return None
            prefix = "HOME"
        else:
            if team not in ("", "AWAY"):
                return None
            prefix = "AWAY"
        root = f"{prefix}_OVER_{int(numeric_line)}_5"
        over = _prob(probs, root)
        label = root + "_" + direction
        p = over if direction == "OVER" else 1.0 - over
    else:
        return None

    if not isfinite(p) or not 0 <= p <= 1 or not 0 <= push <= 1:
        raise ValueError("Invalid win/push probability")
    if p + push > 1.0 + 1e-9:
        raise ValueError("Inconsistent win/draw probabilities")
    return label, p, push
