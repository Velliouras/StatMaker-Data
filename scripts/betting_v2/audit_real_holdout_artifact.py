#!/usr/bin/env python3
"""Audit REAL settled xG/ELO holdout diagnostics from an existing offline QA zip.

Read-only and zero-provider-calls. The outcomes in this zip are ALREADY
OBSERVED and cannot be repackaged as new live pick probabilities or an
independent certificate. Separate xG and ELO populations are never pooled.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile, BadZipFile

REQUIRED = ("pilot_market_holdout.json", "pilot_elo_market_holdout.json",
            "pilot_cached_odds_provenance.json")
MODES = {
    "pilot_market_holdout.json": "XG_PRIMARY",
    "pilot_elo_market_holdout.json": "ELO_GOALS_FALLBACK_NO_XG",
}
FOCUS = ("1X2_HOME", "1X2_AWAY", "MATCH_OVER_2_5_OVER",
         "MATCH_OVER_2_5_UNDER", "MATCH_OVER_3_5_OVER",
         "DOUBLE_CHANCE_HOME_OR_DRAW", "DOUBLE_CHANCE_AWAY_OR_DRAW")


def load_artifact(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > 3_000_000:
        raise ValueError("Offline QA zip missing or too large")
    try:
        with ZipFile(path) as zf:
            if any(name not in zf.namelist() for name in REQUIRED):
                raise ValueError("QA zip does not include required real holdout reports")
            return {name: json.loads(zf.read(name)) for name in REQUIRED}
    except BadZipFile as exc:
        raise ValueError("Invalid QA artifact") from exc


def assess(document: dict) -> dict:
    price = document["pilot_cached_odds_provenance.json"]
    if price.get("contract") != "betting-v2-cached-odds-structural-audit-v1":
        raise ValueError("Price audit has unknown contract")
    results = {}
    for filename, mode in MODES.items():
        report = document[filename]
        if report.get("modelPopulation") != mode:
            raise ValueError("Mixed/mislabelled xG vs ELO holdout")
        if report.get("certified") is not False or report.get("researchOnly") is not True:
            raise ValueError("Unexpected non-research holdout report")
        observed = []
        for market in FOCUS:
            entry = (report.get("byMarket") or {}).get(market)
            if not isinstance(entry, dict):
                continue
            bucket = entry.get("rawProbabilityAtLeast60") or {}
            n = bucket.get("n", 0)
            wins = bucket.get("wins", 0)
            lower = bucket.get("wilson95Lower")
            if (not isinstance(n, int) or not isinstance(wins, int) or
                n < 0 or wins < 0 or wins > n or
                (lower is not None and (not isinstance(lower, (int, float)) or
                                        not 0 <= lower <= 1))):
                raise ValueError("Invalid real holdout observations")
            observed.append({
                "market": market, "rawProbabilityAtLeast60N": n,
                "observedWins": wins,
                "observedHitRate": bucket.get("observedHitRate"),
                "wilson95Lower": lower,
                "lowerBoundAtLeast60": lower is not None and lower >= 0.60,
                "isPricingCertified": False,
            })
        results[mode] = {
            "focusMarkets": observed,
            "quotedROIProven": False,
            "lineupAdversityProven": False,
            "issuedStrong": 0,
        }
    return {
        "contract": "betting-v2-real-holdout-evidence-audit-v1",
        "researchOnly": True,
        "providerCalls": 0,
        "sourceGitHubRunId": 38084468346,
        "populationReports": results,
        "bookmakerTimestampVerified": price.get("priceObservationTimestampVerified") is True,
        "completeIndependentBookmakerUniverseVerified":
            price.get("independentUnfilteredBookmakerUniverseVerified") is True,
        "hasIndividualQuoteEvidence": price.get("quoteIdentityAndAsOfProvenanceIndependentlyVerified") is True,
        "certifiedStrong": 0,
        "blockingReasons": [
            "RAW_MODEL_PROBABILITY_IS_NOT_CALIBRATED_CONSERVATIVE_WIN",
            "PREDICTION_BIN_HOLDOUT_SCORES_ARE_NOT_PREMATCH_BOOKMAKER_ROI",
            "NO_VERIFIED_INDIVIDUAL_BOOKMAKER_PRICE_TIMESTAMPS",
            "NO_UNFILTERED_INDEPENDENT_PRICE_COVERAGE",
            "NO_ADVERSE_LINEUP_SCENARIO_CALIBRATION",
            "NO_OUT_OF_TIME_VERSIONED_PRICE_CERTIFICATE",
        ],
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--offline-qa-zip", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()
    report = assess(load_artifact(args.offline_qa_zip))
    report["sourceArchiveSha256"] = sha256(args.offline_qa_zip.read_bytes()).hexdigest()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                   allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "contract": report["contract"],
        "certifiedStrong": report["certifiedStrong"],
        "modes": list(report["populationReports"]),
        "quotedROIProven": False,
        "providerCalls": 0,
    }))


if __name__ == "__main__":
    main()
