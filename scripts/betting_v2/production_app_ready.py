"""Install direct, evaluated V2 model forecasts into a disposable App-Ready staging DB.

There are no certificates, model-release approvals, artificial provider receipts or
new pipeline/workflow requirements. The existing atomic publish and generation
fingerprint rules remain authoritative. This module does not run on import.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .production_contract import FORECAST_CONTRACT, TIERS, canonical, digest, read_json, require
from .production_sources import full_inventory


SCHEMA = (
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
    require(release["contract"] == "statmaker-betting-v2-canonical-release-v2"
            and release["notYetPublished"] is True, "UNKNOWN_MODEL_FORECAST_RELEASE")
    require(0 < release["producedAtMs"] <= now and bool(release["forecasts"]),
            "NO_REAL_MODEL_FORECASTS")
    documents = release.get("documents", {})
    for sha, document in documents.items():
        require(digest(document) == sha and document.get("researchOnly") is False,
                "INVALID_MODEL_EVIDENCE")
    quotes = {}
    for quote in release.get("quotes", []):
        key = tuple(quote[k] for k in ("competitionId", "snapshotVersion", "fixtureId", "selectionId"))
        require(key not in quotes and quote["generationId"] == release["generationId"],
                "AMBIGUOUS_MODEL_QUOTE")
        require(quote["bookmaker"] and quote["providerQuoteId"]
                and 0 < quote["observedAtMs"] <= release["producedAtMs"]
                and now < quote["validUntilMs"], "STALE_OR_UNATTRIBUTABLE_QUOTE")
        quotes[key] = quote

    observed = set()
    for row in release["forecasts"]:
        key = tuple(row[k] for k in ("competitionId", "snapshotVersion", "fixtureId", "selectionId"))
        require(key not in observed, "DUPLICATE_MODEL_FORECAST")
        observed.add(key)
        f = row["payload"]
        v = f["validation"]
        require(f["contract"] == FORECAST_CONTRACT and f.get("researchOnly") is False
                and f["generationId"] == release["generationId"], "NOT_EVALUATED_MODEL_OUTPUT")
        require(f["fixtureId"] == row["fixtureId"] and f["selectionId"] == row["selectionId"]
                and f["sameScenarioGroup"] and f["leagueCode"] == v["leagueCode"]
                and f["season"] == v["season"] and row["competitionId"] == v["competitionId"],
                "WRONG_MODEL_OR_FIXTURE_IDENTITY")
        require(all(f[field] == v[field] for field in ("modelVersion", "calibrationVersion",
                                                      "marketModelKey", "evidenceMode")),
                "MODEL_EVALUATION_MISMATCH")
        require(v["outOfTimeValidated"] is True and v["comparableMarketCalibrated"] is True
                and v["independentModelErrorIncluded"] is True
                and v["evidenceModeIndependentlyCalibrated"] is True,
                "MISSING_REAL_MODEL_BACKTEST")
        require(0 < v["trainingEndMs"] < v["calibrationStartMs"] <= v["calibrationEndMs"]
                < v["holdoutStartMs"] <= v["holdoutEndMs"] <= v["evaluatedAtMs"]
                <= f["forecastComputedAtMs"] <= release["producedAtMs"] <= now < v["validUntilMs"],
                "LEAKING_OR_STALE_MODEL_EVALUATION")
        require(v["comparableCases"] > 0 and set(v["tierCriteria"]) == set(TIERS),
                "INSUFFICIENT_MARKET_CALIBRATION")
        require(f["kickoffEpochMs"] > now and 1.8 < f["quotedOdd"] < 3.0
                and v["oddsBandFrom"] <= f["quotedOdd"] < v["oddsBandTo"],
                "EXPIRED_FIXTURE_OR_OUTSIDE_ODDS_BAND")
        q = quotes.get(key)
        if q is not None:
            require(q["odd"] == f["quotedOdd"]
                    and q["observedAtMs"] == f["quoteObservedAtEpochMs"]
                    and q["validUntilMs"] == f["quoteValidUntilEpochMs"]
                    and q["validUntilMs"] <= f["kickoffEpochMs"],
                    "CHANGED_EXACT_PROVIDER_QUOTE")
        win, push = f["conservativeWinProbability"], f["conservativePushProbability"]
        require(all(0 <= x <= 1 for x in (win, push))
                and win + push <= 1 + 1e-9
                and win >= 0.60 and win*f["quotedOdd"] + push > 1,
                "STRONG_CALIBRATED_WIN_OR_PUSH_EV_FAILED")
        require(f["centralWinProbability"] >= win and 0 <= f["calibrationReliability"] <= 1
                and 0 <= f["conservativeIntervalWidth"] <= 1,
                "INVALID_MODEL_UNCERTAINTY")
        types = set()
        for feature in f["featureEvidence"]:
            require(feature["feature"] not in types, "DUPLICATE_MODEL_FEATURE")
            types.add(feature["feature"])
            require(len(feature["sourceSha256"]) == 64 and
                    0 < feature["observedAtMs"] <= f["forecastComputedAtMs"]
                    < feature["validUntilMs"], "STALE_OR_UNATTRIBUTABLE_MODEL_FEATURE")
        required = {"ELO", "RECENT_FORM", "LONG_TERM_FORM", "HOME_AWAY", "MARKET_SPECIFIC"}
        if f["evidenceMode"] == "XG_PRIMARY":
            required |= {"XG", "XGA"}
        else:
            require(f.get("primaryXgUnavailableVerified") is True
                    and not types.intersection({"XG", "XGA"}),
                    "INVALID_INDEPENDENT_ELO_FALLBACK")
        require(required <= types, "INCOMPLETE_MARKET_MODEL_INPUTS")
        picked_tier = next((tier for tier in TIERS
                            if win >= v["tierCriteria"][tier]["minimumWin"]
                            and f["calibrationReliability"] >= v["tierCriteria"][tier]["minimumReliability"]
                            and f["conservativeIntervalWidth"] <= v["tierCriteria"][tier]["maximumIntervalWidth"]
                            and v["comparableCases"] >= v["tierCriteria"][tier]["minimumComparableCases"]), None)
        require(picked_tier == f["tier"], "WRONG_EMPIRICAL_STRONG_TIER")


def insert_immutable(db: sqlite3.Connection, table: str, columns: tuple, values: tuple, key_columns: tuple) -> None:
    where = " AND ".join(column+"=?" for column in key_columns)
    key_values = tuple(values[columns.index(column)] for column in key_columns)
    old = db.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {where}", key_values).fetchone()
    if old is not None:
        require(tuple(old) == values, "ATTEMPT_TO_REWRITE_IMMUTABLE_"+table)
        return
    db.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)


def install_release(prepared_db: Path, release_path: Path, repository: Path) -> dict:
    release = read_json(release_path)
    target, checkout = prepared_db.resolve(), repository.resolve()
    # Never mutate checked-in canonical data, the Android database, or a
    # historical issued bet. Only the existing publisher's disposable DB.
    require(prepared_db.is_file() and not prepared_db.is_symlink()
            and not target.is_relative_to(checkout / "data"),
            "ONLY_STAGING_PREPARED_DB_ALLOWED")
    now = int(datetime.now(timezone.utc).timestamp()*1000)
    validate_release(release, now)
    inventory, _ = full_inventory(target, release["generationId"])
    require(digest(inventory) == release["rawInventorySha256"]
            and len(inventory) == release["rawInventoryRowCount"]
            and sum(bool(r["market"]) for r in inventory) == release["rawSelectionCount"],
            "SOURCE_CHANGED_SINCE_MODEL_FORECAST")
    with sqlite3.connect(target) as db:
        versions = dict(db.execute(
            "SELECT competition_id,snapshot_version FROM prepared_snapshot_meta WHERE state='ready'"))
        fingerprint = hashlib.sha256(
            "\n".join(f"{k}|{v}" for k, v in sorted(versions.items())).encode()).hexdigest()
        generation = db.execute(
            """SELECT source_fingerprint,rules_fingerprint FROM prepared_pattern_generation
               WHERE generation_id=? AND state='ready'""",
            (release["generationId"],)).fetchall()
        require(len(generation) == 1 and generation[0][0] == fingerprint
                and hashlib.sha256((fingerprint+"|"+generation[0][1]).encode()).hexdigest()
                    == release["generationId"],
                "MODEL_SOURCE_GENERATION_MISMATCH")
        schema_version = db.execute("PRAGMA user_version").fetchone()[0]
        db.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA:
            db.execute(statement)
        keys = ("generation_id", "competition_id", "snapshot_version", "fixture_id", "selection_id")
        for q in release.get("quotes", []):
            values = tuple(q[k] for k in ("generationId", "competitionId", "snapshotVersion",
                "fixtureId", "selectionId", "bookmaker", "providerQuoteId",
                "observedAtMs", "validUntilMs", "odd"))
            insert_immutable(db, "prepared_betting_v2_quotes",
                keys+("bookmaker", "provider_quote_id", "observed_at_ms", "valid_until_ms", "odd"),
                values, keys)
        for f in release["forecasts"]:
            values = (release["generationId"], f["competitionId"], f["snapshotVersion"],
                      f["fixtureId"], f["selectionId"], canonical(f["payload"]))
            insert_immutable(db, "prepared_betting_v2_forecasts", keys+("payload",), values, keys)
        require(db.execute("PRAGMA user_version").fetchone()[0] == schema_version,
                "UNAUTHORIZED_PREPARED_SCHEMA_VERSION_CHANGE")
    return {"forecasts": len(release["forecasts"]),
            "quotes": len(release.get("quotes", []))}
