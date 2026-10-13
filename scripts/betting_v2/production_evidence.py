"""Direct Kotlin-compatible V2 forecasts from genuine calibrated model evaluation."""
from __future__ import annotations

from .production_contract import (FORECAST_CONTRACT, PRODUCER_VERSION,
                                 Documents, digest, evidence, require)
from .production_evaluation import (UNCERTAINTY_METHOD, bin_for, conservative, tier_for, tail_adjusted_bin)
from .production_models import distribution, probability, unresolved_tail


def model_validation(model: dict, plan: dict, bins: list[dict], adverse_bins: list[dict],
                     policy: dict, results: dict, evaluated_at: int) -> dict:
    """Actual chronological backtest findings embedded in the forecast.

    No certification authority, certificate ID, release approval or invented
    profitability claim. Negative or inconclusive measured ROI is reported,
    not turned into a fake positive certificate.
    """
    require(results["settledCases"] > 0 and not results["revalidationRequired"],
            "MISSING_OR_DRIFTED_MODEL_VALIDATION")
    return {
        "modelVersion": PRODUCER_VERSION + ":" + digest(model),
        "calibrationVersion": digest({"bins": bins, "adverseBins": adverse_bins,
                                       "policy": policy, "plan": plan}),
        "competitionId": plan["competitionId"],
        "leagueCode": plan["leagueCode"],
        "season": plan["season"],
        "marketModelKey": plan["marketModelKey"],
        "evidenceMode": plan["mode"],
        "oddsBandFrom": plan["oddsBandFrom"],
        "oddsBandTo": plan["oddsBandTo"],
        "trainingEndMs": plan["trainingEndMs"],
        "calibrationStartMs": plan["calibrationStartMs"],
        "calibrationEndMs": plan["calibrationEndMs"],
        "holdoutStartMs": plan["holdoutStartMs"],
        "holdoutEndMs": plan["holdoutEndMs"],
        "evaluatedAtMs": evaluated_at,
        "validUntilMs": plan["validUntilMs"],
        "comparableCases": results["settledCases"],
        "tierCriteria": policy,
        "outOfTimeValidated": True,
        "comparableMarketCalibrated": True,
        "independentModelErrorIncluded": True,
        "evidenceModeIndependentlyCalibrated": True,
        "unitStakeRoi": results["unitStakeRoi"],
        "unitStakeRoiLowerBound": results["unitStakeRoiLowerBound"],
        "independentFixtures": results["independentFixtures"],
    }


def forecast(row: dict, case: dict, quote: dict, model: dict, validation: dict,
             bins: list[dict], adverse_bins: list[dict], plan: dict, group: str, documents: Documents, now: int) -> dict:
    require(validation["evaluatedAtMs"] <= now < validation["validUntilMs"], "STALE_MODEL_VALIDATION")
    require(case["adverse"]["validUntilMs"] >= quote["validUntilMs"], "ADVERSE_EXPIRES_BEFORE_QUOTE")
    require(plan["oddsBandFrom"] <= row["odd"] < plan["oddsBandTo"], "UNCALIBRATED_ODDS_COHORT")
    cells = distribution(model, case["features"], plan)
    center = probability(row, cells)
    adverse_cells = distribution(model, case["adverseFeatures"], plan)
    adverse_center = probability(row, adverse_cells)
    bin = tail_adjusted_bin(bin_for(center["win"], bins), unresolved_tail(cells))
    p = conservative(center, bin)
    adverse_bin = tail_adjusted_bin(bin_for(adverse_center["win"], adverse_bins), unresolved_tail(adverse_cells))
    ap = conservative(adverse_center, adverse_bin)
    require(1.8 < quote["odd"] < 3 and p["win"] >= .6 and ap["win"] >= .6,
            "LOW_CONSERVATIVE_WIN_OR_ODDS_OUT_OF_RANGE")
    require(p["win"]*quote["odd"]+p["push"] > 1 and ap["win"]*quote["odd"]+ap["push"] > 1,
            "LOW_CONSERVATIVE_VALUE")
    tier = tier_for(p, bin, validation["tierCriteria"], validation["comparableCases"])
    require(tier is not None, "NO_FROZEN_TIER_QUALIFICATION")
    common = {"fixtureId": row["fixtureId"], "modelVersion": validation["modelVersion"],
              "calibrationVersion": validation["calibrationVersion"], "marketModelKey": row["marketModelKey"]}
    count_cells = [{"home": h, "away": a, "probability": value,
                    "homeOverflow": h == len(cells)-1, "awayOverflow": a == len(cells[0])-1}
                   for h, values in enumerate(cells) for a, value in enumerate(values)]
    # Common score basis: 1X2, DC and DNB all settle against these exact same cells.
    distribution_sha = documents.add(evidence(**common, cells=count_cells,
          overflowRow=len(cells)-1, overflowColumn=len(cells[0])-1,
          marketConsistencyChecks=[{"check": "shared-count-basis", "sum": sum(map(sum, cells)),
                                   "method": "same-cells-for-result-dc-dnb-goals",
                                   "fitFamily": row["family"]}]))
    width = min(1.0, 2*sum(bin["uncertaintyComponents"].values()))
    probability_sha = documents.add(evidence(**common, selectionId=row["selectionId"],
          centralWin=center["win"], conservativeWin=p["win"], conservativePush=p["push"],
          calibrationReliability=bin["calibrationReliability"], conservativeIntervalWidth=width,
          coherentSettlementDistribution=p, uncertaintyComponents=bin["uncertaintyComponents"],
          numericalTailWinUncertainty=unresolved_tail(cells),
          uncertaintyMethodVersion=UNCERTAINTY_METHOD, distributionSha256=distribution_sha))
    valid_until = min(quote["validUntilMs"], validation["validUntilMs"],
                      case["context"]["validUntilMs"], case["adverse"]["validUntilMs"])
    adverse_sha = documents.add(evidence(**common, scenarioId=case["adverse"]["scenarioId"],
          conservativeWin=ap["win"], conservativePush=ap["push"], coherentSettlementDistribution=ap,
          realisticAdverseInputs=case["adverse"]["realisticAdverseInputs"],
          uncertaintyComponents=adverse_bin["uncertaintyComponents"],
          observedAtMs=case["adverse"]["observedAtMs"], validUntilMs=case["adverse"]["validUntilMs"],
          providerRecordSha256=case["adverse"]["providerRecordSha256"]))
    correlation_sha = documents.add(evidence(**common, scenarioGroup=group,
          correlationThreshold=plan["correlationThreshold"], evaluationPlanSha256=digest(plan),
          method="event-indicators-under-shared-fitted-distribution"))
    used = case["history"]
    feature_types = ["ELO", "RECENT_FORM", "LONG_TERM_FORM", "HOME_AWAY", "MARKET_SPECIFIC"]
    if plan["mode"] == "XG_PRIMARY":
        feature_types += ["XG", "XGA"]
    feature_evidence = []
    for feature in feature_types:
        feature_rows = case["strengthHistory"] if feature == "ELO" else used
        observed = max(r["availableAtMs"] for r in feature_rows)
        expires = min(valid_until, observed + model["parameters"]["maximumFeatureAgeMs"])
        require(now < expires, "STALE_FITTED_FEATURE_WINDOW")
        doc = evidence(fixtureId=row["fixtureId"], feature=feature,
              sourceObservationCount=len(feature_rows), observedAtMs=observed, validUntilMs=expires,
              freshnessSemantics="frozen-out-of-time-evaluated-feature-age-policy",
              sourceRecords=[{"fixtureId": str(r["fixture_id"]),
                              "rawStatisticsSha256": r["betting_v2_provenance"]["rawStatisticsSha256"],
                              "availableAtMs": r["availableAtMs"]} for r in feature_rows],
              parametersSha256=digest(model["parameters"]), apiFixtureId=case["apiFixtureId"])
        if feature == "MARKET_SPECIFIC":
            doc["contextProviderRecord"] = case["context"]["providerRecord"]
            doc["contextProviderRecordSha256"] = case["context"]["providerRecordSha256"]
            doc["contextFieldBindings"] = case["context"]["fieldBindings"]
        sha = documents.add(doc)
        feature_evidence.append({"feature": feature, "sourceSha256": sha,
                                 "observedAtMs": observed, "validUntilMs": expires})
    absence_sha = None
    if plan["mode"] == "ELO_GOALS_FALLBACK_NO_XG":
        missing = [r for r in used if "xg" not in r]
        require(bool(missing) and all(r.get("providerXgUnavailable") is True for r in missing),
                "NO_VERIFIED_NO_XG_MODE")
        absence_sha = documents.add(evidence(fixtureId=row["fixtureId"], providerXgUnavailable=True,
              sourceReceiptHashes=[r["betting_v2_provenance"]["rawStatisticsSha256"] for r in missing]))
    return {
        "contract": FORECAST_CONTRACT, "researchOnly": False, **common,
        "generationId": row["generationId"], "selectionId": row["selectionId"],
        "validation": validation, "evidenceMode": plan["mode"],
        "leagueCode": row["leagueCode"], "season": row["season"], "quotedOdd": quote["odd"],
        "quoteObservedAtEpochMs": quote["observedAtMs"], "quoteValidUntilEpochMs": quote["validUntilMs"],
        "forecastComputedAtMs": now, "kickoffEpochMs": row["kickoffEpochMs"],
        "centralWinProbability": center["win"], "conservativeWinProbability": p["win"],
        "conservativePushProbability": p["push"], "adverseScenarioConservativeWinProbability": ap["win"],
        "adverseScenarioPushProbability": ap["push"], "calibrationReliability": bin["calibrationReliability"],
        "conservativeIntervalWidth": width, "tier": tier, "sameScenarioGroup": group,
        "probabilityEvidenceSha256": probability_sha, "featureEvidence": feature_evidence,
        "adverseScenarioEvidenceSha256": adverse_sha, "adverseScenarioId": case["adverse"]["scenarioId"],
        "correlationEvidenceSha256": correlation_sha, "xgUnavailableEvidenceSha256": absence_sha,
        "primaryXgUnavailableVerified": plan["mode"] == "ELO_GOALS_FALLBACK_NO_XG"
              and absence_sha is not None,
    }
