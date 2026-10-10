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


def load_lines(file: Path):
    with file.open(encoding="utf-8") as stream:
        for raw in stream:
            if raw.strip():
                yield json.loads(raw)


def event(quote: dict, forecast: dict) -> tuple[str, float, float, float] | None:
    """Return exact market, WIN, PUSH and realized unit gross return."""
    probabilities = forecast["probabilities"]
    h, a = int(forecast["homeGoals"]), int(forecast["awayGoals"])
    market = str(quote.get("market") or "")
    direction = str(quote.get("direction") or "").upper()
    team = str(quote.get("teamSide") or "").upper()
    line = quote.get("line")
    odd = float(quote["odd"])
    p: float
    win: bool
    push = 0.0
    actual_push = False

    if market == "RESULT_1X2" and direction in {"HOME", "DRAW", "AWAY"}:
        label = f"1X2_{direction}"
        p = float(probabilities[label])
        win = ((h > a and direction == "HOME") or
               (h == a and direction == "DRAW") or
               (h < a and direction == "AWAY"))
    elif market == "RESULT_DNB" and direction in {"HOME", "AWAY"}:
        label = f"1X2_{direction}"
        p = float(probabilities[label])
        push = float(probabilities["1X2_DRAW"])
        win = (h > a and direction == "HOME") or (h < a and direction == "AWAY")
        actual_push = (h == a)
    elif market == "RESULT_DOUBLE_CHANCE" and direction in {
        "HOME_OR_DRAW", "AWAY_OR_DRAW", "HOME_OR_AWAY"
    }:
        if direction == "HOME_OR_DRAW":
            p = float(probabilities["1X2_HOME"]) + float(probabilities["1X2_DRAW"])
            win = h >= a
        elif direction == "AWAY_OR_DRAW":
            p = float(probabilities["1X2_AWAY"]) + float(probabilities["1X2_DRAW"])
            win = a >= h
        else:
            p = 1.0 - float(probabilities["1X2_DRAW"])
            win = h != a
        label = "DOUBLE_CHANCE_" + direction
    elif market in {"FULL_TIME_MATCH_TOTAL", "HOME_TEAM_TOTAL", "AWAY_TEAM_TOTAL"} and (
        line is not None and float(line) in (1.5, 2.5)
    ) and direction in {"OVER", "UNDER"}:
        numeric_line = float(line)
        if market == "FULL_TIME_MATCH_TOTAL" and numeric_line == 2.5:
            label = "MATCH_OVER_2_5"
            over = h + a >= 3
        elif market in {"HOME_TEAM_TOTAL", "AWAY_TEAM_TOTAL"} and numeric_line == 1.5:
            if market == "HOME_TEAM_TOTAL" or team == "HOME":
                label = "HOME_OVER_1_5"
                over = h >= 2
            else:
                label = "AWAY_OVER_1_5"
                over = a >= 2
        else:
            return None
        p_over = float(probabilities[label])
        p = p_over if direction == "OVER" else (1.0 - p_over)
        win = over if direction == "OVER" else not over
        label = label + "_" + direction
    else:
        return None

    # Exact decimal odds settlement with explicit DNB push.
    return label, p, push, odd if win else (1.0 if actual_push else 0.0)


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
        unique = (ident, q.get("selectionKey"))
        if unique in seen:
            rejected["duplicate_exact_price"] += 1
            continue
        seen.add(unique)
        try:
            odd = float(q.get("odd"))
        except (ValueError, TypeError):
            rejected["invalid_price"] += 1
            continue
        if not 1.8 < odd < 3.0:
            rejected["outside_odds_contract"] += 1
            continue
        forecasted = event(q, predicted)
        if forecasted is None:
            rejected["unsupported_market_in_research_baseline"] += 1
            continue
        market, p, push, gross = forecasted
        if not 0 <= p <= 1 or not 0 <= push <= 1 or p + push > 1.0 + 1e-9:
            rejected["invalid_probability"] += 1
            continue
        rows.append({
            "fixture": ident, "selectionKey": q.get("selectionKey"),
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
