"""Opt-in canonical App-Ready population, BEFORE the existing bundle/hash/atomic publisher.

No network, manifest rewrite, fingerprint override, or training inside the publisher.
Only an explicitly approved immutable release can alter a disposable staging SQLite.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .production_contract import (CERTIFICATE_CONTRACT, EVIDENCE_CONTRACT, FORECAST_CONTRACT,
                                 TIERS, canonical, digest, file_digest, read_json, require)
from .production_sources import full_inventory

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS prepared_betting_v2_certificates(
       certification_id TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS prepared_betting_v2_evidence(
       sha256 TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS prepared_betting_v2_quotes(
       generation_id TEXT NOT NULL, competition_id TEXT NOT NULL, snapshot_version TEXT NOT NULL,
       fixture_id TEXT NOT NULL, selection_id TEXT NOT NULL, bookmaker TEXT NOT NULL,
       provider_quote_id TEXT NOT NULL, observed_at_ms INTEGER NOT NULL,
       valid_until_ms INTEGER NOT NULL, odd REAL NOT NULL,
       PRIMARY KEY(generation_id,competition_id,snapshot_version,fixture_id,selection_id))""",
    """CREATE TABLE IF NOT EXISTS prepared_betting_v2_forecasts(
       generation_id TEXT NOT NULL, competition_id TEXT NOT NULL, snapshot_version TEXT NOT NULL,
       fixture_id TEXT NOT NULL, selection_id TEXT NOT NULL, payload TEXT NOT NULL,
       PRIMARY KEY(generation_id,competition_id,snapshot_version,fixture_id,selection_id))""",
)


def validate_release(release: dict, now: int) -> None:
    require(release["contract"] == "statmaker-betting-v2-canonical-release-v1"
            and release["notYetPublished"] is True, "UNKNOWN_CANONICAL_RELEASE")
    require(0 < release["producedAtMs"] <= now, "INVALID_RELEASE_TIME")
    require(bool(release["forecasts"]) and bool(release["certificates"]), "NO_GENUINE_CERTIFIED_RELEASE")
    docs = release["documents"]
    for sha, doc in docs.items():
        require(doc["contract"] == EVIDENCE_CONTRACT and doc.get("researchOnly") is False
                and digest(doc) == sha, "INVALID_RELEASE_EVIDENCE")
    certs = {}
    for cert in release["certificates"]:
        require(cert["contract"] == CERTIFICATE_CONTRACT and cert.get("researchOnly") is False,
                "INVALID_RELEASE_CERTIFICATE")
        id = cert["certificationId"]
        require(id not in certs, "DUPLICATE_CERTIFICATION_ID")
        require(0 < cert["trainingEndMs"] < cert["calibrationStartMs"] <= cert["calibrationEndMs"]
                <= cert["frozenPolicyAtMs"] < cert["untouchedHoldoutStartMs"] <= cert["untouchedHoldoutEndMs"]
                <= cert["evaluatedAtMs"] <= release["producedAtMs"] <= now < cert["validUntilMs"],
                "LEAKING_OR_STALE_CERTIFICATE")
        require(cert["comparableCases"] > 0 and set(cert["tierCriteria"]) == set(TIERS), "NO_FROZEN_TIER_POLICY")
        for field in ("reportSha256", "reliabilityDiagramSha256", "modelErrorAnalysisSha256",
                      "driftAnalysisSha256", "profitabilityAnalysisSha256"):
            doc = docs[cert[field]]
            require(all(doc[k] == cert[k] for k in ("modelVersion", "calibrationVersion",
                                                   "marketModelKey", "evidenceMode")),
                    "CROSS_MODEL_EVIDENCE_JOIN")
        report = docs[cert["reportSha256"]]
        for key in ("trainingEndMs", "calibrationStartMs", "calibrationEndMs",
                    "untouchedHoldoutStartMs", "untouchedHoldoutEndMs", "trainingFingerprint"):
            require(report[key] == cert[key], "REPORT_CERTIFICATE_MISMATCH")
        require(report["frozenTierCriteria"] == cert["tierCriteria"]
                and report["settledCases"] == cert["comparableCases"], "UNFROZEN_OR_MISMATCHED_POLICY")
        profit = docs[cert["profitabilityAnalysisSha256"]]
        require(profit["unitStakeRoiLowerBound"] > 0 and profit["settledCases"] == cert["comparableCases"]
                and profit["priceDatasetSha256"] in docs, "NO_INDEPENDENT_PRICED_PROFITABILITY")
        require(docs[cert["driftAnalysisSha256"]]["revalidationRequired"] is False, "DRIFT_REVALIDATION_REQUIRED")
        require(bool(docs[cert["reliabilityDiagramSha256"]]["bins"])
                and bool(docs[cert["reliabilityDiagramSha256"]]["adverseBins"])
                and docs[cert["modelErrorAnalysisSha256"]]["outOfTimeCases"] == cert["comparableCases"],
                "NO_INDEPENDENT_ERROR_CALIBRATION")
        require(bool(docs[cert["modelErrorAnalysisSha256"]]["adverseErrorBySegment"]),
                "NO_INDEPENDENT_ADVERSE_ERROR_EVIDENCE")
        certs[id] = cert
    quotes = {}
    for q in release["quotes"]:
        key = tuple(q[k] for k in ("competitionId", "snapshotVersion", "fixtureId", "selectionId"))
        require(key not in quotes and q["generationId"] == release["generationId"], "AMBIGUOUS_RELEASE_QUOTE")
        require(q["bookmaker"] and q["providerQuoteId"]
                and 0 < q["observedAtMs"] <= release["producedAtMs"] <= now < q["validUntilMs"],
                "STALE_OR_UNATTRIBUTABLE_QUOTE")
        quotes[key] = q
    seen = set()
    for row in release["forecasts"]:
        key = tuple(row[k] for k in ("competitionId", "snapshotVersion", "fixtureId", "selectionId"))
        require(key not in seen, "AMBIGUOUS_RELEASE_FORECAST")
        seen.add(key)
        f, q = row["payload"], quotes[key]
        cert = certs[f["certificationId"]]
        require(f["contract"] == FORECAST_CONTRACT and f.get("researchOnly") is False
                and f["generationId"] == release["generationId"], "WRONG_FORECAST_CONTRACT")
        require(all(f[k] == cert[k] for k in ("leagueCode", "season", "marketModelKey", "modelVersion", "evidenceMode"))
                and row["competitionId"] == cert["competitionId"], "CROSS_COHORT_FORECAST")
        require(f["fixtureId"] == row["fixtureId"] and f["selectionId"] == row["selectionId"],
                "FORECAST_IDENTITY_MISMATCH")
        require(f["forecastComputedAtMs"] == release["producedAtMs"]
                and f["kickoffEpochMs"] > now and q["validUntilMs"] <= f["kickoffEpochMs"]
                and f["quotedOdd"] == q["odd"] and f["quoteObservedAtEpochMs"] == q["observedAtMs"]
                and f["quoteValidUntilEpochMs"] == q["validUntilMs"]
                and cert["oddsBandFrom"] <= q["odd"] < cert["oddsBandTo"] and 1.8 < q["odd"] < 3,
                "STALE_OR_CHANGED_EXACT_PRICE")
        require(f["conservativeWinProbability"] >= .6 and f["adverseScenarioConservativeWinProbability"] >= .6
                and f["conservativeWinProbability"]*q["odd"]+f["conservativePushProbability"] > 1
                and f["adverseScenarioConservativeWinProbability"]*q["odd"]+f["adverseScenarioPushProbability"] > 1,
                "STRONG_CONSERVATIVE_GATE_FAILED")
        probability = docs[f["probabilityEvidenceSha256"]]
        require(probability["fixtureId"] == row["fixtureId"] and probability["selectionId"] == row["selectionId"]
                and probability["modelVersion"] == cert["modelVersion"]
                and probability["calibrationVersion"] == cert["calibrationVersion"], "PROBABILITY_BINDING_MISMATCH")
        require(probability["centralWin"] == f["centralWinProbability"]
                and probability["conservativeWin"] == f["conservativeWinProbability"]
                and probability["conservativePush"] == f["conservativePushProbability"]
                and probability["calibrationReliability"] == f["calibrationReliability"]
                and probability["conservativeIntervalWidth"] == f["conservativeIntervalWidth"],
                "FORECAST_PROBABILITY_MISMATCH")
        model_errors = docs[cert["modelErrorAnalysisSha256"]]
        require(model_errors["uncertaintyMethodVersion"] == probability["uncertaintyMethodVersion"]
                and all(0 <= v for v in probability["uncertaintyComponents"].values()),
                "MISSING_EMPIRICAL_UNCERTAINTY_BINDING")
        distribution = docs[probability["distributionSha256"]]
        require(distribution["fixtureId"] == row["fixtureId"]
                and distribution["modelVersion"] == cert["modelVersion"]
                and distribution["marketModelKey"] == cert["marketModelKey"]
                and distribution["cells"] and distribution["marketConsistencyChecks"],
                "MISSING_SHARED_DISTRIBUTION_EVIDENCE")
        dist = probability["coherentSettlementDistribution"]
        require(abs(sum(dist.values())-1) <= 1e-9 and all(0 <= v <= 1 for v in dist.values()),
                "INCOHERENT_WIN_PUSH_LOSE")
        adverse = docs[f["adverseScenarioEvidenceSha256"]]
        require(adverse["fixtureId"] == row["fixtureId"] and adverse["scenarioId"] == f["adverseScenarioId"]
                and adverse["calibrationVersion"] == cert["calibrationVersion"]
                and adverse["realisticAdverseInputs"] and adverse["observedAtMs"] <= f["forecastComputedAtMs"]
                and adverse["validUntilMs"] >= q["validUntilMs"], "UNPROVEN_ADVERSE_SCENARIO")
        require(adverse["conservativeWin"] == f["adverseScenarioConservativeWinProbability"]
                and adverse["conservativePush"] == f["adverseScenarioPushProbability"]
                and abs(sum(adverse["coherentSettlementDistribution"].values())-1) <= 1e-9,
                "INCOHERENT_ADVERSE_DISTRIBUTION")
        correlation = docs[f["correlationEvidenceSha256"]]
        require(correlation["fixtureId"] == row["fixtureId"]
                and correlation["modelVersion"] == cert["modelVersion"]
                and correlation["scenarioGroup"] == f["sameScenarioGroup"], "UNPROVEN_CORRELATION")
        types = set()
        for feature in f["featureEvidence"]:
            require(feature["feature"] not in types, "DUPLICATE_FEATURE")
            types.add(feature["feature"])
            doc = docs[feature["sourceSha256"]]
            require(doc["fixtureId"] == row["fixtureId"] and doc["feature"] == feature["feature"]
                    and doc["sourceObservationCount"] > 0
                    and 0 < doc["observedAtMs"] == feature["observedAtMs"] <= f["forecastComputedAtMs"]
                    and doc["validUntilMs"] == feature["validUntilMs"] > now, "STALE_OR_MISSING_FEATURE_LINEAGE")
        required = {"ELO", "RECENT_FORM", "LONG_TERM_FORM", "HOME_AWAY", "MARKET_SPECIFIC"}
        if f["evidenceMode"] == "XG_PRIMARY":
            required |= {"XG", "XGA"}
        else:
            absence = docs[f["xgUnavailableEvidenceSha256"]]
            require(absence["fixtureId"] == row["fixtureId"] and absence["providerXgUnavailable"] is True
                    and not types.intersection({"XG", "XGA"}), "UNPROVEN_NO_XG_FALLBACK")
        require(required <= types, "MISSING_REQUIRED_FEATURE")
        assessed_tier = next((tier for tier in TIERS
            if f["conservativeWinProbability"] >= cert["tierCriteria"][tier]["minimumWin"]
            and f["calibrationReliability"] >= cert["tierCriteria"][tier]["minimumReliability"]
            and f["conservativeIntervalWidth"] <= cert["tierCriteria"][tier]["maximumIntervalWidth"]
            and cert["comparableCases"] >= cert["tierCriteria"][tier]["minimumComparableCases"]), None)
        require(assessed_tier == f["tier"], "UNFROZEN_OR_FORGED_TIER")


def insert_immutable(db: sqlite3.Connection, table: str, columns: tuple, values: tuple, key_columns: tuple) -> None:
    where = " AND ".join(column+"=?" for column in key_columns)
    key_values = tuple(values[columns.index(column)] for column in key_columns)
    old = db.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {where}", key_values).fetchone()
    if old is not None:
        require(tuple(old) == values, "ATTEMPT_TO_REWRITE_IMMUTABLE_"+table)
        return
    db.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)


def install_release(prepared_db: Path, release_path: Path, approval_path: Path, repository: Path) -> dict:
    approval, release = read_json(approval_path), read_json(release_path)
    require(approval["contract"] == "statmaker-betting-v2-owner-release-approval-v1"
            and approval["approvedForCanonicalAppReady"] is True and approval["approvedBy"],
            "MISSING_EXPLICIT_RELEASE_APPROVAL")
    require(approval["releaseSha256"] == file_digest(release_path)
            and approval["generationId"] == release["generationId"], "UNAPPROVED_RELEASE_DIGEST")
    target, staging = prepared_db.resolve(), Path(approval["stagingRoot"]).resolve()
    require(target.is_relative_to(staging) and not target.is_relative_to(repository.resolve()/"data")
            and prepared_db.is_file() and not prepared_db.is_symlink(), "ONLY_DISPOSABLE_STAGING_DB_ALLOWED")
    now = int(datetime.now(timezone.utc).timestamp()*1000)
    validate_release(release, now)
    inventory, _ = full_inventory(target, release["generationId"])
    require(digest(inventory) == release["rawInventorySha256"]
            and len(inventory) == release["rawInventoryRowCount"]
            and sum(bool(r["market"]) for r in inventory) == release["rawSelectionCount"],
            "SOURCE_CHANGED_SINCE_FORECAST")
    with sqlite3.connect(target) as db:
        versions = dict(db.execute("SELECT competition_id,snapshot_version FROM prepared_snapshot_meta WHERE state='ready'"))
        fingerprint = hashlib.sha256("\n".join(f"{k}|{v}" for k, v in sorted(versions.items())).encode()).hexdigest()
        generation = db.execute("""SELECT source_fingerprint,rules_fingerprint FROM prepared_pattern_generation
                                WHERE generation_id=? AND state='ready'""", (release["generationId"],)).fetchall()
        require(len(generation) == 1 and generation[0][0] == fingerprint
                and hashlib.sha256((fingerprint+"|"+generation[0][1]).encode()).hexdigest() == release["generationId"],
                "EXISTING_GENERATION_FINGERPRINT_MISMATCH")
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        db.execute("BEGIN IMMEDIATE")
        for sql in SCHEMA:
            db.execute(sql)
        for sha, doc in release["documents"].items():
            insert_immutable(db, "prepared_betting_v2_evidence", ("sha256", "payload"),
                             (sha, canonical(doc)), ("sha256",))
        for cert in release["certificates"]:
            insert_immutable(db, "prepared_betting_v2_certificates", ("certification_id", "payload", "sha256"),
                             (cert["certificationId"], canonical(cert), digest(cert)), ("certification_id",))
        keys = ("generation_id", "competition_id", "snapshot_version", "fixture_id", "selection_id")
        for q in release["quotes"]:
            values = tuple(q[k] for k in ("generationId", "competitionId", "snapshotVersion", "fixtureId",
                     "selectionId", "bookmaker", "providerQuoteId", "observedAtMs", "validUntilMs", "odd"))
            insert_immutable(db, "prepared_betting_v2_quotes", keys+("bookmaker", "provider_quote_id",
                             "observed_at_ms", "valid_until_ms", "odd"), values, keys)
        for f in release["forecasts"]:
            values = (release["generationId"], f["competitionId"], f["snapshotVersion"],
                      f["fixtureId"], f["selectionId"], canonical(f["payload"]))
            insert_immutable(db, "prepared_betting_v2_forecasts", keys+("payload",), values, keys)
        require(db.execute("PRAGMA user_version").fetchone()[0] == schema, "UNAUTHORIZED_SCHEMA_VERSION_CHANGE")
    return {"certificates": len(release["certificates"]), "forecasts": len(release["forecasts"]),
            "quotes": len(release["quotes"]), "evidence": len(release["documents"])}
