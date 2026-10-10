#!/usr/bin/env python3
"""Research-only strict exact-price replay against untouched goals/1X2 holdout.

No forecast certificate is emitted. Never use these raw probabilities to label
a Betting V2 STRONG without independent out-of-time calibration and lineup
scenario validation.

Usage:
 python research/betting_v2/evaluate_holdout_prices.py \
   --forecasts /tmp/v2-holdout.jsonl \
   --quotes /tmp/v2-prices.jsonl \
   --report /tmp/v2-odds-holdout.json
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

from quote_join import exact_join, forecast_index
from forecast_market import market_probability


def load_lines(file: Path):
    with file.open(encoding="utf-8") as stream:
        for raw in stream:
            if raw.strip():
                yield json.loads(raw)


def event(quote: dict, forecast: dict) -> tuple[str, float, float, float] | None:
    """RESEARCH-ONLY: settle a PREMATCH probability against a finished score.

    Market selection and probabilities are delegated to forecast_market.py
    (which never reads outcomes), preventing live/research market drift.
    """
    mapped = market_probability(quote, forecast)
    if mapped is None:
        return None
    label, p, push = mapped
    h, a = int(forecast["homeGoals"]), int(forecast["awayGoals"])
    odd = float(quote["odd"])
    market = str(quote.get("market") or "")
    direction = str(quote.get("direction") or "").upper()
    actual_push = False

    if market == "RESULT_1X2":
        won = ((direction == "HOME" and h > a) or
               (direction == "DRAW" and h == a) or
               (direction == "AWAY" and h < a))
    elif market == "RESULT_DNB":
        actual_push = h == a
        won = ((direction == "HOME" and h > a) or
               (direction == "AWAY" and h < a))
    elif market == "RESULT_DOUBLE_CHANCE":
        won = ((direction == "HOME_OR_DRAW" and h >= a) or
               (direction == "AWAY_OR_DRAW" and a >= h) or
               (direction == "HOME_OR_AWAY" and h != a))
    else:
        line = float(quote["line"])
        score = (h + a if market == "FULL_TIME_MATCH_TOTAL" else
                 h if market == "HOME_TEAM_TOTAL" else a)
        over = score > line
        won = over if direction == "OVER" else not over
    return label, p, push, odd if won else (1.0 if actual_push else 0.0)


def evaluate(forecasts: list[dict], quotes: list[dict]) -> dict:
    by_fixture = forecast_index(forecasts)
    rows = []
    rejected = defaultdict(int)
    seen = set()
    for q in quotes:
        predicted, join_status = exact_join(q, by_fixture)
        if predicted is None:
            rejected[join_status] += 1
            continue
        ident = (str(q["leagueCode"]), str(q["fixtureId"]))
        selection_key = str(q.get("selectionKey") or "").strip()
        if not selection_key:
            rejected["missing_exact_selection_key"] += 1
            continue
        unique = (ident, selection_key)
        if unique in seen:
            rejected["duplicate_exact_price"] += 1
            continue
        try:
            odd = float(q.get("odd"))
        except (ValueError, TypeError):
            rejected["invalid_price"] += 1
            continue
        if not 1.8 < odd < 3.0:
            rejected["outside_odds_contract"] += 1
            continue
        try:
            forecasted = event(q, predicted)
        except (KeyError, ValueError, TypeError, OverflowError):
            rejected["malformed_market_probability_or_score"] += 1
            continue
        if forecasted is None:
            rejected["unsupported_market_in_research_baseline"] += 1
            continue
        market, p, push, gross = forecasted
        if not 0 <= p <= 1 or not 0 <= push <= 1 or p + push > 1.0 + 1e-9:
            rejected["invalid_probability"] += 1
            continue
        seen.add(unique)
        rows.append({
            "fixture": ident, "selectionKey": selection_key,
            "market": market, "odd": odd, "p": p, "push": push,
            "expectedReturn": p * odd + push,
            "grossReturn": gross
        })

    # Exploratory only: no event can be promoted to STRONG from this screen.
    # Restrict the study to the same user probability/price target; these are
    # RAW model probabilities, not calibrated conservative bounds.
    candidates = [x for x in rows if x["p"] >= 0.6 and x["expectedReturn"] > 1.0]
    by_fixture = defaultdict(list)
    for row in candidates:
        by_fixture[row["fixture"]].append(row)
    # One correlated score scenario per fixture; no opportunistic jackpot EV sort.
    dedup = [sorted(pool, key=lambda x: (-x["p"], -x["expectedReturn"],
                                        str(x["selectionKey"])))[0]
             for pool in by_fixture.values()]

    def summary(items: list[dict]) -> dict:
        n = len(items)
        if n == 0:
            return {"n": 0, "roi": None}
        outcomes = sum(x["grossReturn"] for x in items)
        return {
            "n": n,
            "averageOdds": round(sum(x["odd"] for x in items) / n, 4),
            "meanUncalibratedP": round(sum(x["p"] for x in items) / n, 4),
            "won": sum(x["grossReturn"] > 1.0 for x in items),
            "lost": sum(x["grossReturn"] == 0.0 for x in items),
            "push": sum(x["grossReturn"] == 1.0 for x in items),
            "roi": round(outcomes / n - 1.0, 5),
            "grossReturnUnits": round(outcomes, 4)
        }

    markets = defaultdict(list)
    for row in rows:
        markets[row["market"]].append(row)
    return {
        "contract": "betting-v2-goals-price-research-v1",
        "notCertified": True,
        "unverifiedModelUncertainty": True,
        "noRealisticAdverseLineupScenario": True,
        "notValidForPromotion": True,
        "note": "Quotes must be archived pre-kickoff odds; raw 60% is NOT the conservative 60% V2 requirement.",
        "availableExactJoinedQuotes": len(rows),
        "rejected": dict(rejected),
        "allResearchQuotes": summary(rows),
        "rawProbabilityAndValueCandidates": summary(candidates),
        "oneCorrelatedScenarioPerMatch": summary(dedup),
        "byMarket": {name: summary(items) for name, items in sorted(markets.items())}
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forecasts", type=Path, required=True)
    parser.add_argument("--quotes", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(list(load_lines(args.forecasts)), list(load_lines(args.quotes)))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print("Written:", args.report)
    print("This is research only. Certified STRONG picks: 0.")


if __name__ == "__main__":
    main()
