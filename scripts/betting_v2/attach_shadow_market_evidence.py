#!/usr/bin/env python3
"""Attach REAL cached bookmaker prices to frozen research probabilities.

No API calls and NEVER creates certified tips. Provider event ID+league+both
team identities+absolute kickoff must uniquely agree. Only supported full-time
1X2, double chance, match goals and team goals are mapped. No Asian, handicap,
corners or other unmodeled markets. Provider quote updatedAt is not independently
verified; indicative p*odd-1 is NOT a certified edge or guaranteed ROI.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import argparse
import json

from fixture_lookup import canonical_name, as_utc

SOURCE = Path("reports/betting_v2/pilot_forward_shadow_preview.json")
ODDS = Path("odds/odds_api_io/domestic_odds.json")
MAX_FEED_BYTES = 90_000_000
GOAL_LINES = {0.5, 1.5, 2.5, 3.5, 4.5, 5.5}
MIN_ODD = 1.50
MAX_ODD = 4.50


def probability(m: dict, row: dict) -> tuple[str, float] | None:
    """A market model mapping must be exact and support a coherent binary result."""
    market = str(row.get("market") or "").strip().upper()
    selection = str(row.get("selection") or "").strip().upper()
    probs = m.get("probabilities") or {}
    key = ""
    p = None
    if market == "1X2":
        key = {"HOME": "1X2_HOME", "DRAW": "1X2_DRAW",
               "AWAY": "1X2_AWAY"}.get(selection, "")
        p = probs.get(key)
    elif market == "DOUBLE_CHANCE":
        keys = {"1X": ("1X2_HOME", "1X2_DRAW"),
                "X2": ("1X2_DRAW", "1X2_AWAY"),
                "12": ("1X2_HOME", "1X2_AWAY")}.get(selection)
        if keys:
            key = selection
            if all(isinstance(probs.get(k), (int, float))
                   and not isinstance(probs[k], bool) for k in keys):
                p = sum(probs[k] for k in keys)
    elif market in ("MATCH_GOALS", "TEAM_TOTAL_GOALS"):
        line = row.get("line")
        try:
            numeric = float(line) if not isinstance(line, bool) else None
        except (TypeError, ValueError, OverflowError):
            numeric = None
        if numeric not in GOAL_LINES or selection not in ("OVER", "UNDER"):
            return None
        side = "MATCH"
        if market == "TEAM_TOTAL_GOALS":
            team = canonical_name(row.get("team"))
            if not team:
                return None
            home = {canonical_name(m.get("homeTeam")),
                    canonical_name(m.get("canonicalHomeTeam"))} - {""}
            away = {canonical_name(m.get("awayTeam")),
                    canonical_name(m.get("canonicalAwayTeam"))} - {""}
            if team in home and team not in away:
                side = "HOME"
            elif team in away and team not in home:
                side = "AWAY"
            else:
                return None
        key = f"{side}_OVER_{int(numeric)}_5"
        over = probs.get(key)
        if isinstance(over, (int, float)) and not isinstance(over, bool):
            p = float(over) if selection == "OVER" else 1.0 - float(over)
        key = f"{market}_{side}_{selection}_{numeric:g}"
    else:
        return None
    if isinstance(p, bool) or not isinstance(p, (float, int)):
        return None
    p = float(p)
    if not isfinite(p) or not 0.0 <= p <= 1.0:
        return None
    return key, p


def pair_matches(forecast: dict, item: dict, league_code: str) -> bool:
    if str(forecast.get("leagueCode") or "") != league_code:
        return False
    if str(forecast.get("providerEventId") or "") != str(item.get("id") or ""):
        return False
    first = as_utc(forecast.get("kickoffUTC"))
    second = as_utc(item.get("kickoff") or "")
    if first is None or second is None or (
        abs((first-second).total_seconds()) > 900
    ):
        return False
    if item.get("teamMappingStatus") != "matched" or (
        item.get("scheduleVerified") is not True
    ):
        return False
    return (canonical_name(forecast.get("homeTeam")) ==
            canonical_name(item.get("providerHomeTeam")) and
            canonical_name(forecast.get("awayTeam")) ==
            canonical_name(item.get("providerAwayTeam")))


def evidence(forecast: dict, match: dict) -> list[dict]:
    """At most one highest exact bookmaker price per mapped market selection."""
    found: dict[tuple, dict] = {}
    for item in match.get("markets") or []:
        if not isinstance(item, dict) or item.get("exactBookmakerOdds") is not True:
            continue
        try:
            odd = float(item.get("odds"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not isfinite(odd) or not MIN_ODD <= odd <= MAX_ODD:
            continue
        bookmaker = str(item.get("bookmaker") or "").strip()
        if not bookmaker or len(bookmaker)>90:
            continue
        mapped = probability(forecast, item)
        if mapped is None:
            continue
        key, p = mapped
        raw_ev = p * odd - 1.0
        if not isfinite(raw_ev):
            continue
        ident = (str(item.get("market") or ""),
                 str(item.get("selection") or ""),
                 str(item.get("line") or ""),
                 canonical_name(item.get("team")))
        offer = {
            "market": str(item.get("market")),
            "selection": str(item.get("selection")),
            "line": item.get("line"),
            "bookmaker": bookmaker,
            "odds": round(odd, 3),
            "modelProbability": round(p, 6),
            "indicativeEVNotCertified": round(raw_ev, 5),
            "certifiedStrong": False,
            "bookmakerPriceUpdatedAtVerified": False,
            "EV_Certified": False,
        }
        old = found.get(ident)
        if old is None or offer["odds"] > old["odds"]:
            found[ident] = offer
    # Balance market families rather than sorting all picks by uncalibrated
    # high EV. One maximum per family, all marked research-only.
    family_best: dict[str, dict] = {}
    for offer in found.values():
        family = offer["market"]
        previous = family_best.get(family)
        if previous is None or (
            offer["indicativeEVNotCertified"], offer["modelProbability"]
        ) > (
            previous["indicativeEVNotCertified"], previous["modelProbability"]
        ):
            family_best[family] = offer
    return [family_best[k] for k in (
        "1X2", "DOUBLE_CHANCE", "MATCH_GOALS", "TEAM_TOTAL_GOALS"
    ) if k in family_best]


def attach(root: Path) -> dict:
    root = root.resolve()
    preview = root / SOURCE
    odds_path = root / ODDS
    result = json.loads(preview.read_text(encoding="utf-8"))
    if (result.get("contract") != "statmaker-v2-uat-forecast-preview-v1"
        or result.get("certificationStatus") != "BLOCKED"
        or result.get("certifiedStrong") != 0
        or result.get("researchOnly") is not True):
        raise ValueError("Missing research-only preview contract")
    if not odds_path.is_file() or odds_path.stat().st_size > MAX_FEED_BYTES:
        result["priceEvidenceStatus"] = "CANONICAL_ODDS_FEED_NOT_ACCESSIBLE"
        return result
    feed = json.loads(odds_path.read_text(encoding="utf-8"))
    if not isinstance(feed, dict):
        raise ValueError("Malformed canonical odds feed")
    by_event: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for league in feed.get("leagues") or []:
        if not isinstance(league, dict):
            continue
        code = str(league.get("leagueCode") or "")
        for item in league.get("matches") or []:
            if isinstance(item, dict) and item.get("id"):
                by_event[code, str(item["id"])].append(item)
    joined = priced = 0
    for forecast in result.get("matches") or []:
        if not isinstance(forecast, dict):
            continue
        matches = [item for item in by_event.get(
            (forecast.get("leagueCode"), forecast.get("providerEventId")), []
        ) if pair_matches(forecast, item, str(forecast["leagueCode"]))]
        if len(matches) != 1:
            forecast["marketEvidence"] = []
            continue
        joined += 1
        offers = evidence(forecast, matches[0])
        forecast["marketEvidence"] = offers
        if offers:
            priced += 1
    result["priceEvidenceStatus"] = "UNVERIFIED_QUOTE_FRESHNESS_RESEARCH_ONLY"
    result["sameProviderLiveOddsFixtureLinks"] = joined
    result["fixturesWithSupportedMarketEvidence"] = priced
    result["bookmakerOddsCertified"] = False
    result["EV_Certified"] = False
    result["ROI_Certified"] = False
    result["certifiedStrong"] = 0
    result["apiCalls"] = 0
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    args = ap.parse_args()
    root = args.repository_root.resolve()
    result = attach(root)
    (root/SOURCE).write_text(json.dumps(result, ensure_ascii=False,
                                        allow_nan=False, indent=2)+"\n",
                             encoding="utf-8")
    print(json.dumps({"quotedFixtures":result.get("fixturesWithSupportedMarketEvidence",0),
                      "sameProviderMatches":result.get("sameProviderLiveOddsFixtureLinks",0),
                      "certifiedStrong":0,"providerCalls":0}))


if __name__ == "__main__":
    main()
