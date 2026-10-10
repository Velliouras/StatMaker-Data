#!/usr/bin/env python3
"""Read-only, no-price market diagnostics for untouched Betting V2 holdouts.

NOT a betting signal or certification. Markets within a fixture are correlated.
Reports separate ELO fallback and primary xG populations; never mixes them.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from math import isfinite

from calibration_gate import (
    model_events, build_calibration, prob_bin, MIN_MARKET_BIN, MIN_LOCAL_BIN
)
from elo_holdout_diagnostics import wilson_lower

MODES = frozenset({"XG_PRIMARY", "ELO_GOALS_FALLBACK_NO_XG"})


def _summary(events: list[tuple[float, int]]) -> dict:
    n = len(events)
    high = [(p, outcome) for p, outcome in events if p >= 0.60]
    wins = sum(outcome for _, outcome in events)
    hwins = sum(outcome for _, outcome in high)
    return {
        "n": n,
        "observedWins": wins,
        "observedHitRate": wins / n if n else None,
        "meanRawProbability": sum(p for p, _ in events) / n if n else None,
        "brier": sum((p - result) ** 2 for p, result in events) / n if n else None,
        "rawProbabilityAtLeast60": {
            "n": len(high),
            "wins": hwins,
            "observedHitRate": hwins / len(high) if high else None,
            "wilson95Lower": wilson_lower(hwins, len(high)) if high else None,
            "observedWilson95LowerAtLeast60": bool(
                high and wilson_lower(hwins, len(high)) >= .60
            ),
            "certified": False,
        },
    }


def summarize(calibration: list[dict], holdout: list[dict], mode: str) -> dict:
    """Audit only raw forecasts whose labels are strictly later than calibration.

    The descriptive Wilson bound is not certified predictive reliability:
    no lineup adversity, independent bookmaker odds or ROI has been shown.
    """
    if mode not in MODES:
        raise ValueError("Unknown Betting V2 model population")
    if not calibration or not holdout:
        raise ValueError("Calibration and holdout must both be nonempty")
    if max(str(x["date"]) for x in calibration) >= min(str(x["date"]) for x in holdout):
        raise ValueError("Chronological calibration and holdout overlap")
    for row in [*calibration, *holdout]:
        strategy = row.get("strategy")
        if mode == "ELO_GOALS_FALLBACK_NO_XG":
            if strategy != mode or row.get("historicalXgSufficient") is not False:
                raise ValueError("Primary/xG evidence cannot calibrate ELO fallback")
        elif strategy not in (None, "XG_PRIMARY"):
            raise ValueError("Fallback/unknown strategy cannot calibrate xG model")

    # Freeze calibration BEFORE evaluating the later holdout. Readiness here
    # means statistical pre-price evidence only, NEVER STRONG certification.
    bins = build_calibration(calibration)
    passed_by_market: dict[str, int] = defaultdict(int)
    blocked: dict[str, int] = defaultdict(int)
    candidate_fixtures: set[tuple[str, str]] = set()

    global_markets: dict[str, list[tuple[float, int]]] = defaultdict(list)
    league_markets: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    rejected: dict[str, int] = defaultdict(int)
    for match in holdout:
        league = str(match.get("league") or "")
        code = str(match.get("leagueCode") or "")
        fixture = str(match.get("fixtureId") or "")
        if not league or not code or not fixture:
            rejected["missing_fixture_or_league_identity"] += 1
            continue
        identity = (code, fixture)
        if identity in seen:
            rejected["duplicate_fixture"] += 1
            continue
        seen.add(identity)
        try:
            date.fromisoformat(str(match["date"]))
            rows = model_events(match)
        except (KeyError, ValueError, TypeError, OverflowError):
            rejected["invalid_outcome_or_market"] += 1
            continue
        for market, probability, hit in rows:
            if (not isfinite(probability) or probability < 0 or probability > 1 or
                    hit not in (0, 1)):
                rejected["invalid_probability_or_binary_result"] += 1
                continue
            global_markets[market].append((probability, hit))
            league_markets[(league, market)].append((probability, hit))
            # Not a bet: no bookmaker offer, quote timestamp or lineup bound.
            # This is a frozen-calibration market/league eligibility counter.
            bucket = prob_bin(probability)
            g_wins, g_n = bins["global"].get((market, bucket), (0, 0))
            l_wins, l_n = bins["local"].get((league, market, bucket), (0, 0))
            if probability < 0.60:
                blocked["raw_probability_below_60pct"] += 1
            elif g_n < MIN_MARKET_BIN:
                blocked["insufficient_global_market_bin"] += 1
            elif l_n < MIN_LOCAL_BIN:
                blocked["insufficient_league_market_bin"] += 1
            elif min(wilson_lower(g_wins, g_n),
                     wilson_lower(l_wins, l_n)) < 0.60:
                blocked["conservative_wilson_lower_below_60pct"] += 1
            else:
                passed_by_market[market] += 1
                candidate_fixtures.add(identity)
    return {
        "contract": "betting-v2-unpriced-market-holdout-v1",
        "modelPopulation": mode,
        "researchOnly": True,
        "certified": False,
        "strongRecommendations": 0,
        "hasVerifiedIndependentBookmakerPrices": False,
        "hasAdverseLineupScenarioCertification": False,
        "hasROIProof": False,
        "outcomesUsedOnlyForRetrospectiveEvaluation": True,
        "marketCorrelationsMayInvalidateNaivePooledSignificance": True,
        "frozenCalibrationReadiness": {
            "method": "prior-date only global market/bin >=70; same-league market/bin >=25; min 95pct Wilson >=0.60",
            "prePriceOnly": True,
            "hasBookmakerQuoteOrExpectedValueEvidence": False,
            "hasAdverseLineupBounds": False,
            "certifiedStrong": 0,
            "minGlobalBin": MIN_MARKET_BIN,
            "minLeagueBin": MIN_LOCAL_BIN,
            "passedMarketEvaluations": sum(passed_by_market.values()),
            "fixturesWithAtLeastOnePassingMarket": len(candidate_fixtures),
            "passedByMarket": dict(sorted(passed_by_market.items())),
            "blockedCounts": dict(sorted(blocked.items())),
        },
        "rejected": dict(sorted(rejected.items())),
        "byMarket": {
            key: _summary(items) for key, items in sorted(global_markets.items())
        },
        "byLeagueAndMarket": {
            f"{league}::{market}": _summary(items)
            for (league, market), items in sorted(league_markets.items())
        },
    }
