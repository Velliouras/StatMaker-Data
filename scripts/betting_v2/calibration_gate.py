#!/usr/bin/env python3
"""Conservative empirical calibration diagnostic (research only, NEVER certificate).

1. Use model forecasts on chronological CALIBRATION matches to construct
   Wilson 95% lower win-rate bounds for each (market, predicted probability bin).
2. Apply those FROZEN calibration bounds to chronologically later HOLDOUT
   markets offered at archived exact prices.
3. Report strict >1.80, <3.00 and >=0.60 lower-bound selection performance.

Market- and league-specific diagnostics are printed separately. Low-sample
strata are rejected rather than widened with hidden arbitrary priors.
This is not lineup validation, does not independently certify model quality
and does not generate any app-ready artifact.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from math import sqrt
from pathlib import Path

from evaluate_holdout_prices import event, load_lines

MIN_MARKET_BIN = 70
MIN_LOCAL_BIN = 25


def prob_bin(probability: float) -> int:
    return min(9, max(0, int(probability * 10)))


def wilson_lower(successes: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0:
        return 0.0
    p = successes / trials
    denom = 1 + z * z / trials
    adjusted = p + z * z / (2 * trials)
    margin = z * sqrt((p * (1 - p) + z * z / (4 * trials)) / trials)
    return max(0.0, (adjusted - margin) / denom)


def model_events(match: dict) -> list[tuple[str, float, int]]:
    """Create actual 1X2, team/match goals and under calibration events."""
    probabilities = match["probabilities"]
    actual = match["observed"]
    results = ("1X2_HOME", "1X2_DRAW", "1X2_AWAY")
    totals = ("HOME_OVER_1_5", "AWAY_OVER_1_5", "MATCH_OVER_2_5")
    events = [
        (key, float(probabilities[key]), int(actual[key]))
        for key in results if key in probabilities and key in actual
    ]
    for key in totals:
        if key in probabilities and key in actual:
            p, y = float(probabilities[key]), int(actual[key])
            events.append((key + "_OVER", p, y))
            events.append((key + "_UNDER", 1.0 - p, 1 - y))
    for key, components, predicate in (
        ("DOUBLE_CHANCE_HOME_OR_DRAW", ("1X2_HOME", "1X2_DRAW"),
         lambda h, a: h >= a),
        ("DOUBLE_CHANCE_AWAY_OR_DRAW", ("1X2_AWAY", "1X2_DRAW"),
         lambda h, a: a >= h),
        ("DOUBLE_CHANCE_HOME_OR_AWAY", ("1X2_HOME", "1X2_AWAY"),
         lambda h, a: h != a),
    ):
        if all(x in probabilities for x in components):
            p = sum(float(probabilities[x]) for x in components)
            events.append((key, p, int(predicate(
                int(match["homeGoals"]), int(match["awayGoals"])))) )
    return events


def build_calibration(calibration: list[dict]) -> dict:
    global_counts = defaultdict(lambda: [0, 0])
    local_counts = defaultdict(lambda: [0, 0])
    for match in calibration:
        league = str(match.get("league") or "")
        for market, p, result in model_events(match):
            if not 0 <= p <= 1:
                continue
            bin_key = (market, prob_bin(p))
            global_counts[bin_key][0] += result
            global_counts[bin_key][1] += 1
            local_counts[(league, *bin_key)][0] += result
            local_counts[(league, *bin_key)][1] += 1
    return {"global": dict(global_counts), "local": dict(local_counts)}


def match_quoted_event(quote: dict, forecast: dict):
    """Map exact bookmaker identity to calibration model event name."""
    got = event(quote, forecast)
    if got is None:
        return None
    market, raw_p, push, gross = got
    # DNB has an explicit PUSH; this simple Wilson calibration is strictly
    # for binary markets. Do not silently treat push as a win.
    if market.startswith("1X2_") and quote.get("market") == "RESULT_DNB":
        return None
    return market, raw_p, push, gross


def evaluate(calibration: list[dict], holdout: list[dict],
             quotes: list[dict]) -> dict:
    params = build_calibration(calibration)
    by_id = {(str(f["date"]), str(f["fixtureId"])): f for f in holdout}
    rejected = defaultdict(int)
    qualifying = []
    per_market = defaultdict(lambda: [0, 0])
    for quote in quotes:
        if quote.get("fixtureId") is None:
            rejected["missing_fixture_id"] += 1
            continue
        match = by_id.get((str(quote.get("date")), str(quote["fixtureId"])))
        if match is None:
            rejected["not_in_untouched_holdout"] += 1
            continue
        try:
            odd = float(quote["odd"])
        except (KeyError, ValueError, TypeError):
            rejected["bad_odds"] += 1
            continue
        if not 1.8 < odd < 3.0:
            rejected["outside_price_contract"] += 1
            continue
        value = match_quoted_event(quote, match)
        if value is None:
            rejected["not_supported_binary_research_market"] += 1
            continue
        market, raw_p, push, gross = value
        if push != 0:
            rejected["unvalidated_push_calibration"] += 1
            continue
        prob_bucket = prob_bin(raw_p)
        success, size = params["global"].get((market, prob_bucket), (0, 0))
        league = str(match.get("league") or "")
        local_success, local_size = params["local"].get(
            (league, market, prob_bucket), (0, 0))
        per_market[market][0] += 1
        if size < MIN_MARKET_BIN or local_size < MIN_LOCAL_BIN:
            rejected["insufficient_market_or_league_calibration"] += 1
            continue
        conservative = min(
            wilson_lower(success, size),
            wilson_lower(local_success, local_size)
        )
        if conservative < 0.6:
            rejected["conservative_probability_below_60pct"] += 1
            continue
        if conservative * odd <= 1.0:
            rejected["lower_expected_value_not_positive"] += 1
            continue
        per_market[market][1] += 1
        qualifying.append({
            "date": match["date"], "fixtureId": str(match["fixtureId"]),
            "market": market, "selectionKey": str(quote.get("selectionKey")),
            "odd": odd, "gross": gross, "rawP": raw_p,
            "conservativeCalibratedP": conservative
        })
    # Distinct market bets involving the score distribution share a latent
    # goals scenario. Research reports both all and one-per-fixture.
    by_fixture = defaultdict(list)
    for candidate in qualifying:
        by_fixture[(candidate["date"], candidate["fixtureId"])].append(candidate)
    noncorrelated = [sorted(xs, key=lambda x: (
        -x["conservativeCalibratedP"], -x["rawP"], x["selectionKey"]
    ))[0] for xs in by_fixture.values()]

    def metrics(xs: list[dict]) -> dict:
        if not xs:
            return {"n": 0, "roi": None, "winRate": None}
        n = len(xs)
        return {
            "n": n,
            "won": sum(x["gross"] > 1 for x in xs),
            "lost": sum(x["gross"] == 0 for x in xs),
            "winRate": sum(x["gross"] > 1 for x in xs) / n,
            "roi": sum(x["gross"] - 1.0 for x in xs) / n,
            "averageOdds": sum(x["odd"] for x in xs) / n,
            "meanConservativeP": sum(x["conservativeCalibratedP"] for x in xs) / n
        }
    return {
        "contract": "statmaker-v2-research-calibration-replay-v1",
        "certified": False,
        "calibration": {"fixtures": len(calibration),
                        "minGlobalSamples": MIN_MARKET_BIN,
                        "minLeagueSamples": MIN_LOCAL_BIN,
                        "method": "95% Wilson lower bound by market/bucket and league"},
        "holdout": {"fixtures": len(holdout),
                    "exactQuotes": len(quotes)},
        "rejected": dict(rejected),
        "marketCounts": {k: {"evaluated": v[0], "qualified": v[1]}
                         for k, v in sorted(per_market.items())},
        "allRawQualifiedOffers": metrics(qualifying),
        "oneGoalsScenarioPerFixture": metrics(noncorrelated),
        "blockingGaps": [
            "No reliable adverse injury/lineup scenario calculation",
            "Small sample and multiple-comparison risk needs evaluation",
            "No proof historical feature extraction matched original as-of releases",
            "Not a certified new-production forecast source"
        ]
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calibration-forecasts", required=True, type=Path)
    ap.add_argument("--holdout-forecasts", required=True, type=Path)
    ap.add_argument("--quotes", required=True, type=Path)
    ap.add_argument("--report", required=True, type=Path)
    args = ap.parse_args()
    calibration = list(load_lines(args.calibration_forecasts))
    holdout = list(load_lines(args.holdout_forecasts))
    quotes = list(load_lines(args.quotes))
    if not calibration or not holdout:
        raise ValueError("Both calibration and holdout fixture sets must be nonempty")
    latest_calibration = max(x["date"] for x in calibration)
    first_holdout = min(x["date"] for x in holdout)
    if latest_calibration >= first_holdout:
        raise ValueError("Chronological calibration/holdout leakage")
    result = evaluate(calibration, holdout, quotes)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    print("Independent calibration/holdout research:", args.report)
    print("Live V2 recommendations published: 0")


if __name__ == "__main__":
    main()
