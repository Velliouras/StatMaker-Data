#!/usr/bin/env python3
"""Precompute REAL forward Betting V2 UAT suggestions from frozen models + bookmaker quotes.

Input is the published, actual-data shadow preview produced from archived
pre-kickoff xG/ELO model forecasts and the canonical Odds-API.io bookmaker feed.
No mock events, provider calls, invented odds, retroactive results, or Android
production writes. Only independently matched API-Football IDs participate.
The research-only grade is SOLID / provisional, NOT certified STRONG, until
model calibration, price freshness and independently audited EV are proven.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import json

from fixture_lookup import as_utc

SOURCE = Path("reports/betting_v2/pilot_forward_shadow_preview.json")
DESTINATION = Path("reports/betting_v2/betting_v2_precompute.json")
CONTRACT = "statmaker-v2-uat-real-precompute-v1"
MARKETS = frozenset({"1X2", "DOUBLE_CHANCE", "MATCH_GOALS", "TEAM_TOTAL_GOALS"})
MIN_ODD = 1.50
MAX_ODD = 3.00
MIN_PROBABILITY = 0.45
MIN_INDICATIVE_EV = 0.05
MAX_BYTES = 3_000_000


def candidate(match: dict, *, now: datetime, rejects: Counter) -> dict | None:
    kickoff = as_utc(match.get("kickoffUTC"))
    forecast_at = as_utc(match.get("forecastComputedAtUTC"))
    if kickoff is None or forecast_at is None or not forecast_at < kickoff or kickoff <= now:
        rejects["NOT_UPCOMING_PREMATCH_FORECAST"] += 1
        return None
    if not match.get("independentApiFootballFixtureVerified") or not (
        str(match.get("independentApiFootballFixtureId") or "").strip()
    ):
        rejects["MISSING_INDEPENDENT_FIXTURE_ID"] += 1
        return None
    if match.get("certifiedStrong") is not False:
        rejects["UNSAFE_CERTIFICATION_FLAG"] += 1
        return None
    if (str(match.get("model") or "") not in
            ("XG_PRIMARY", "ELO_GOALS_FALLBACK_NO_XG")):
        rejects["UNKNOWN_MODEL"] += 1
        return None

    best: dict | None = None
    quality = -1.0
    for offer in match.get("marketEvidence") or []:
        if not isinstance(offer, dict):
            continue
        market = str(offer.get("market") or "")
        if market not in MARKETS or (
            offer.get("certifiedStrong") is not False or
            offer.get("EV_Certified") is not False or
            offer.get("bookmakerPriceUpdatedAtVerified") is not False
        ):
            continue
        if not str(offer.get("bookmaker") or "").strip():
            continue
        try:
            p = float(offer["modelProbability"])
            odd = float(offer["odds"])
            indicative_ev = float(offer["indicativeEVNotCertified"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not all(map(isfinite, (p, odd, indicative_ev))):
            continue
        if not (MIN_ODD <= odd <= MAX_ODD and MIN_PROBABILITY <= p <= 1.0 and
                indicative_ev >= MIN_INDICATIVE_EV):
            continue
        if abs(p * odd - 1.0 - indicative_ev) > 0.005:
            rejects["PRICE_PROBABILITY_EV_INCONSISTENT"] += 1
            continue
        if market == "MATCH_GOALS" or market == "TEAM_TOTAL_GOALS":
            try:
                line = float(offer.get("line"))
            except (ValueError, TypeError, OverflowError):
                continue
            if line not in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5):
                continue
        score = p * 0.70 + min(indicative_ev, 0.20) * 0.30
        if score > quality:
            best, quality = offer, score

    if best is None:
        rejects["NO_ELIGIBLE_REAL_MARKET"] += 1
        return None

    return {
        "leagueCode": match["leagueCode"],
        "providerEventId": str(match["providerEventId"]),
        "apiFootballFixtureId": str(match["independentApiFootballFixtureId"]),
        "homeTeam": str(match["homeTeam"]),
        "awayTeam": str(match["awayTeam"]),
        "kickoffUTC": kickoff.isoformat(),
        "forecastComputedAtUTC": forecast_at.isoformat(),
        "model": match["model"],
        "grade": "SOLID",
        "gradeStatus": "UAT_PROVISIONAL_NO_STRONG_CERTIFICATION",
        "market": best["market"],
        "selection": str(best["selection"]),
        "line": best.get("line"),
        "team": str(best.get("team") or ""),
        "bookmaker": str(best["bookmaker"]),
        "odds": float(best["odds"]),
        "probability": float(best["modelProbability"]),
        "indicativeEV": float(best["indicativeEVNotCertified"]),
        "qualityScore": round(quality, 6),
        "certifiedStrong": False,
        "EV_Certified": False,
        "bookmakerPriceUpdatedAtVerified": False,
    }


def compute(document: dict, *, now: datetime) -> dict:
    if now.utcoffset() is None:
        raise ValueError("Precompute timestamp must be timezone aware")
    now = now.astimezone(timezone.utc)
    if (document.get("contract") != "statmaker-v2-uat-forecast-preview-v1"
        or document.get("certificationStatus") != "BLOCKED"
        or document.get("researchOnly") is not True
        or document.get("certifiedStrong") != 0
        or document.get("bookmakerOddsCertified") is not False
        or document.get("EV_Certified") is not False
        or not isinstance(document.get("matches"), list)):
        raise ValueError("Missing real-data research-only source contract")
    rejects = Counter()
    rows = []
    matches = []
    seen = set()
    for original in document["matches"]:
        if not isinstance(original, dict):
            continue
        match = dict(original)
        identity = (str(match.get("leagueCode") or ""),
                    str(match.get("providerEventId") or ""))
        if identity in seen:
            rejects["DUPLICATE_FIXTURE"] += 1
            continue
        seen.add(identity)
        selected = candidate(match, now=now, rejects=rejects)
        match["precomputedSelection"] = None
        if selected:
            match["precomputedSelection"] = {
                "market": selected["market"],
                "selection": selected["selection"],
                "line": selected["line"],
                "team": selected["team"],
                "bookmaker": selected["bookmaker"],
                "odds": selected["odds"],
                "modelProbability": selected["probability"],
                "indicativeEVNotCertified": selected["indicativeEV"],
                "grade": selected["grade"],
                "gradeStatus": selected["gradeStatus"],
                "certifiedStrong": False,
                "EV_Certified": False,
                "bookmakerPriceUpdatedAtVerified": False,
            }
            rows.append(selected)
        matches.append(match)
    rows.sort(key=lambda x: (-x["qualityScore"], x["kickoffUTC"],
                             x["leagueCode"], x["providerEventId"]))
    return {
        "contract": CONTRACT,
        "generatedAtUTC": now.isoformat(),
        "forecastComputedAtUTC": document.get("forecastComputedAtUTC"),
        "sourceArchive": document.get("sourceArchive"),
        "modelVersion": "statmaker-v2-shadow-goals-xg-elo",
        "dataSource": "REAL_ARCHIVED_PREMATCH_XG_ELO_AND_CANONICAL_BOOKMAKER_ODDS",
        "researchOnly": True,
        "readOnly": True,
        "certificationStatus": "BLOCKED",
        "certifiedStrong": 0,
        "bookmakerOddsCertified": False,
        "EV_Certified": False,
        "ROI_Certified": False,
        "apiCalls": 0,
        "thresholds": {
            "minOdd": MIN_ODD, "maxOdd": MAX_ODD,
            "minProbability": MIN_PROBABILITY,
            "minIndicativeEV": MIN_INDICATIVE_EV,
            "independentFixtureIdRequired": True,
            "oneSelectionPerFixture": True,
        },
        "suggestionCount": len(rows),
        "realMatchedFixtureCount": sum(bool(m.get("marketEvidence")) for m in matches),
        "independentlyVerifiedFixtureCount": sum(
            bool(m.get("independentApiFootballFixtureVerified")) for m in matches
        ),
        "rejected": dict(sorted(rejects.items())),
        "suggestions": rows,
        "matches": matches,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    args = parser.parse_args()
    root = args.repository_root.resolve()
    path = root / SOURCE
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Real-data source preview is missing or oversized")
    document = json.loads(path.read_text(encoding="utf-8"))
    output = compute(document, now=datetime.now(timezone.utc))
    destination = root / DESTINATION
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2,
                                      allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "contract": CONTRACT,
        "realPrematchUatSuggestions": output["suggestionCount"],
        "realMatchesWithBookmakerOdds": output["realMatchedFixtureCount"],
        "independentlyVerifiedFixtures": output["independentlyVerifiedFixtureCount"],
        "rejected": output["rejected"],
        "apiCalls": 0, "certifiedStrong": 0,
    }))


if __name__ == "__main__":
    main()
