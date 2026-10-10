#!/usr/bin/env python3
"""Post-hoc ELO-only holdout diagnostics, never a forecast certificate.

Inputs are pregame research forecasts with historical outcome labels. Outcomes
are read ONLY for retrospective evaluation. This module does not alter ratings,
training, calibration, selections, or any production artifacts.
"""
from __future__ import annotations

from collections import defaultdict
from math import isfinite, sqrt


KEYS = ("1X2_HOME", "1X2_DRAW", "1X2_AWAY")


def wilson_lower(wins: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0 or wins < 0 or wins > trials:
        return 0.0
    p = wins / trials
    denominator = 1.0 + z * z / trials
    adjusted = p + z * z / (2.0 * trials)
    error = z * sqrt((p * (1.0 - p) + z * z / (4.0 * trials)) / trials)
    return max(0.0, (adjusted - error) / denominator)


def _metrics(data: list[tuple[bool, bool, float]]) -> dict:
    total = len(data)
    correct = sum(hit for hit, _, _ in data)
    raw_high = [(hit, p) for hit, over, p in data if over]
    high_n = len(raw_high)
    high_wins = sum(hit for hit, _ in raw_high)
    return {
        "n": total,
        "argmaxWins": correct,
        "argmaxAccuracy": correct / total if total else None,
        "rawTop1X2ProbabilityAtLeast60": {
            "n": high_n,
            "wins": high_wins,
            "observedHitRate": high_wins / high_n if high_n else None,
            "wilson95Lower": wilson_lower(high_wins, high_n) if high_n else None,
            "meanRawP": sum(p for _, p in raw_high) / high_n if high_n else None,
            "conservative60Demonstrated": bool(high_n and wilson_lower(high_wins, high_n) >= 0.60),
            "certified": False,
        },
        "certified": False,
        "hasPriceOrEVValidation": False,
    }


def summarize_elo_holdout(predictions: list[dict]) -> dict:
    """Report held-out ELO fallback wins without market-price or lineup claims."""
    global_items = []
    leagues: dict[str, list[tuple[bool, bool, float]]] = defaultdict(list)
    rejected = defaultdict(int)
    for row in predictions:
        if row.get("strategy") != "ELO_GOALS_FALLBACK_NO_XG" or row.get("historicalXgSufficient") is not False:
            rejected["not_elo_fallback_only"] += 1
            continue
        probs = row.get("probabilities") or {}
        observed = row.get("observed") or {}
        try:
            p = [float(probs[key]) for key in KEYS]
            result = [int(observed[key]) for key in KEYS]
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected["missing_market_probability_or_outcome"] += 1
            continue
        if (not all(isfinite(v) and 0.0 <= v <= 1.0 for v in p) or
                abs(sum(p) - 1.0) > 1e-5 or
                any(y not in (0, 1) for y in result) or sum(result) != 1):
            rejected["inconsistent_distribution_or_result"] += 1
            continue
        best = max(range(3), key=lambda i: p[i])
        event = (result[best] == 1, p[best] >= .60, p[best])
        global_items.append(event)
        leagues[str(row.get("league") or "UNKNOWN")].append(event)
    return {
        "contract": "betting-v2-elo-only-retrospective-holdout-v1",
        "researchOnly": True,
        "notCertified": True,
        "notEnoughForStrongWithoutPricedMarketAndAdverseScenario": True,
        "method": "raw top-1X2, 95pct Wilson lower bound; holdout outcome labels only",
        "rejected": dict(sorted(rejected.items())),
        "overall": _metrics(global_items),
        "leagues": {key: _metrics(value) for key, value in sorted(leagues.items())},
    }
