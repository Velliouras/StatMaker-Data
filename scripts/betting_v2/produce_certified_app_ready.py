"""Explicit OFFLINE phases. No imports run training, write data, publish, or contact providers."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .production_contract import (Blocked, Documents, digest, file_digest, milliseconds, read_json,
                                 require, save_new, selection_id, verify_plan)
from .production_sources import (cached_history, full_inventory, market_identity, quote_for, resolve_fixture, card_history)
from .production_models import (calibrate_distribution, event_outcome, features, fit_count_model,
                                select_parameters, context_values, distribution)
from .production_evaluation import (evaluate_holdout, freeze_tiers, observed_predictions,
                                    scenario_groups, uncertainty_bins)
from .production_evidence import certify, forecast
from .production_quote_adapter import adapt_quote_receipts
from .production_context import adapt_context_receipts, verify_receipt


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp()*1000)


def safe_workspace(path: Path, repository: Path) -> Path:
    root = path.resolve()
    require(not root.is_relative_to(repository.resolve()/"data")
            and not root.is_relative_to(repository.resolve()/".github")
            and not root.is_relative_to(repository.resolve()/"reports"), "PROTECTED_OR_RESEARCH_OUTPUT")
    root.mkdir(parents=True, exist_ok=True)
    return root


def contexts_index(path: Path) -> dict:
    doc = read_json(path)
    require(doc["contract"] == "statmaker-betting-v2-point-in-time-context-v1"
            and doc.get("researchOnly") is not True, "MISSING_REAL_CONTEXT_SOURCE")
    index = {}
    for c in doc["records"]:
        key = str(c["fixtureId"])
        require(key not in index, "DUPLICATE_CONTEXT_FIXTURE")
        require(bool(c["providerRecord"]) and digest(c["providerRecord"]) == c["providerRecordSha256"],
                "CONTEXT_SOURCE_HASH_MISMATCH")
        index[key] = c
    return index


def input_source(path: Path) -> dict:
    source = read_json(path)
    require(source["contract"] == "statmaker-betting-v2-prod-input-v1"
            and source["unfilteredRawInventorySha256"] == digest(source["rawRows"])
            and source["rawSelectionCount"] == sum(bool(r["market"]) for r in source["rawRows"])
            and source["rawFixtureCount"] == len({(r["competitionId"], r["fixtureId"]) for r in source["rawRows"]}),
            "INVALID_OR_FILTERED_CANONICAL_INPUT")
    return source


def case_for(history: list[dict], target: dict, context: dict, offer: dict,
             parameters: dict, plan: dict, at: int) -> dict:
    base = context["base"]
    require(base["scenarioKind"] == "BASE" and context["adverse"]["scenarioKind"] == "ADVERSE_DOUBTFUL",
            "CONTEXT_BASE_ADVERSE_REVERSED")
    require(context["fixtureId"] == str(target["fixture_id"]), "WRONG_CONTEXT_FIXTURE")
    x, used, strength_history = features(history, target, at, plan["family"], plan["mode"], parameters,
                       base, plan["contextColumns"])
    require(bool(used) and at-max(r["availableAtMs"] for r in used) < parameters["maximumFeatureAgeMs"],
            "STALE_HISTORICAL_FEATURES")
    adverse = context["adverse"]
    require(bool(adverse["realisticAdverseInputs"]) and adverse["scenarioId"], "NO_GROUNDED_ADVERSE_INPUTS")
    adverse_x = list(x)
    adverse_x[-len(plan["contextColumns"]):] = context_values(
        adverse, target, at, plan["contextColumns"])
    require(adverse_x != x, "ADVERSE_SCENARIO_IS_NOT_A_REALISTIC_CHANGE")
    require(all(item["scenario"] == "provider-doubtful-player-unavailable"
                for item in adverse["realisticAdverseInputs"]), "UNGROUNDED_ADVERSE_SCENARIO")
    doubtful = {str(item["player"]["id"]) for item in adverse["providerRecord"]["receipt"]["response"]
                if str(item["fixture"]["id"]) == str(target["fixture_id"])
                and item["player"]["type"] == "Questionable"}
    require({i["playerId"] for i in adverse["realisticAdverseInputs"]} == doubtful,
            "ADVERSE_PLAYER_SOURCE_MISMATCH")
    return {"features": x, "adverseFeatures": adverse_x, "context": base, "adverse": adverse,
            "history": used, "strengthHistory": strength_history, "apiFixtureId": str(target["fixture_id"]), "atMs": at,
            "offer": offer, "sourceHashes": sorted({r["sourceSha256"] for r in used+strength_history}
                         | {base["providerRecordSha256"], adverse["providerRecordSha256"]}),
            "seasonTransition": any(r["season"] != target["season"] for r in used)}


def model_rows(source: dict, contexts: dict, params: dict, plan: dict, through: int) -> tuple[list[dict], Counter]:
    result, reasons = [], Counter()
    history = card_history(source["history"], plan["cardCountContract"]) if plan["family"] == "CARDS" else source["history"]
    for target in history:
        if target["leagueCode"] != plan["leagueCode"]:
            continue
        try:
            context = contexts[str(target["fixture_id"])]
            at = milliseconds(context["base"]["observedAtMs"])
            if not plan["trainingStartMs"] <= at <= through or target["availableAtMs"] > through:
                continue
            require(plan["family"] in target["counts"], "MISSING_OBSERVED_COUNT_LABEL")
            case = case_for(history, target, context, {}, params, plan, at)
            result.append(dict(case, target=target["counts"][plan["family"]],
                               labelAvailableAtMs=target["availableAtMs"]))
        except (Blocked, KeyError, TypeError) as error:
            reasons[str(error)] += 1
    return result, reasons


def priced_cases(source: dict, contexts: dict, quotations: list[dict], params: dict,
                 plan: dict, start: int, end: int) -> tuple[list[dict], Counter]:
    history_rows = card_history(source["history"], plan["cardCountContract"]) if plan["family"] == "CARDS" else source["history"]
    history = {(r["leagueCode"], str(r["fixture_id"])): r for r in history_rows}
    result, reasons, seen = [], Counter(), set()
    # Predeclared FIRST_OBSERVED quote policy; no price/outcome cherry-picking.
    require(plan["historicalQuoteDecisionPolicy"] == "FIRST_OBSERVED", "UNFROZEN_PRICE_SAMPLING_POLICY")
    for q in sorted(quotations, key=lambda r: r["observedAtMs"]):
        raw = q["canonicalOffer"]
        try:
            at = milliseconds(q["forecastAsOfMs"])
            if not start <= at <= end:
                continue
            row = market_identity(raw)
            row["selectionId"] = selection_id(row)
            if (row["marketModelKey"] != plan["marketModelKey"]
                    or row["competitionId"] != plan["competitionId"]
                    or row["leagueCode"] != plan["leagueCode"] or row["season"] != plan["season"]):
                continue
            key = (row["competitionId"], row["fixtureId"], row["selectionId"])
            if key in seen:
                reasons["REPEATED_QUOTE_NOT_AN_INDEPENDENT_CASE"] += 1
                continue
            seen.add(key)
            target = history[(row["leagueCode"], str(q["apiFootballFixtureId"]))]
            require(target["season"] == row["season"]
                    and target["kickoffEpochMs"] == row["kickoffEpochMs"]
                    and str(target["home_team_id"]) == str(q["homeTeamId"])
                    and str(target["away_team_id"]) == str(q["awayTeamId"]), "HISTORICAL_CROSSWALK_MISMATCH")
            verified_quote = quote_for(row, [q], at)
            if plan["family"] == "CARDS":
                require(q["bookmaker"] == plan["cardCountContract"]["bookmaker"]
                        and q["cardCountContractSha256"] == digest(plan["cardCountContract"]),
                        "CARD_QUOTE_SETTLEMENT_CONTRACT_MISMATCH")
            require(plan["oddsBandFrom"] <= row["odd"] < plan["oddsBandTo"], "OUTSIDE_FROZEN_ODDS_COHORT")
            context = contexts[str(target["fixture_id"])]
            realized = context["adverse"].get("historicallyRealized") is True
            if realized:
                verify_realization(context["adverse"], target)
            case = case_for(history_rows, target, context, row, params, plan, at)
            label = target["counts"][plan["family"]]
            result.append(dict(case, quote=verified_quote, target=label,
                               historicalAdverseRealized=realized,
                               outcome=event_outcome(row, *label), labelAvailableAtMs=target["availableAtMs"]))
        except (Blocked, KeyError, TypeError) as error:
            reasons[str(error)] += 1
    return result, reasons


def inventory(args) -> None:
    workspace = safe_workspace(args.workspace, args.repository_root)
    raw, issues = full_inventory(args.prepared_db, args.generation_id)
    history, schedule, source_issues = cached_history(args.repository_root)
    save_new(workspace/"source.json", {"contract": "statmaker-betting-v2-prod-input-v1",
             "generationId": args.generation_id, "rawRows": raw, "history": history, "schedule": schedule,
             "rawSelectionCount": sum(bool(r["market"]) for r in raw),
             "rawFixtureCount": len({(r["competitionId"], r["fixtureId"]) for r in raw}),
             "inventoryIssues": dict(issues), "historicalSourceIssues": dict(source_issues),
             "unfilteredRawInventorySha256": digest(raw), "preparedDbSha256": file_digest(args.prepared_db)})


def quotes(args) -> None:
    import gzip
    source = input_source(args.source)
    receipts = []
    for path in args.receipts:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            receipts.append(json.load(stream))
    adapted, issues = adapt_quote_receipts(source, receipts, read_json(args.attestations), now_ms())
    save_new(args.output, {"contract": "statmaker-betting-v2-production-quote-input-v1",
                          "quotes": adapted, "rejected": dict(issues)})


def verify_realization(adverse: dict, target: dict) -> None:
    receipt = adverse["realizationReceipt"]
    verify_receipt(receipt)
    require(digest(receipt) == adverse["realizationReceiptSha256"]
            and adverse["observedAtMs"] <= receipt["clientReceivedAtMs"] < target["kickoffEpochMs"],
            "UNPROVEN_ADVERSE_REALIZATION_TIME")
    matches = [f for f in receipt["response"] if str(f["fixture"]["id"]) == str(target["fixture_id"])]
    require(len(matches) == 1, "UNPROVEN_REALIZATION_FIXTURE")
    lineup = {str(b["team"]["id"]): b.get("startXI", []) for b in matches[0].get("lineups", [])}
    teams = [str(target[k]) for k in ("home_team_id", "away_team_id")]
    require(all(t in lineup and len(lineup[t]) == 11 for t in teams), "NO_ACTUAL_CONFIRMED_XI")
    players = {str(p["player"]["id"]) for t in teams for p in lineup[t]}
    require(not players.intersection(i["playerId"] for i in adverse["realisticAdverseInputs"]),
            "ADVERSE_SCENARIO_NOT_HISTORICALLY_REALIZED")


def contexts(args) -> None:
    records, rejected = adapt_context_receipts(input_source(args.source),
                         [read_json(p) for p in args.receipts], args.maximum_age_ms)
    save_new(args.output, {"contract": "statmaker-betting-v2-point-in-time-context-v1",
                          "researchOnly": False, "records": records, "rejected": dict(rejected)})


def freeze(args) -> None:
    source, plan = input_source(args.source), read_json(args.plan)
    verify_plan(plan)
    now = now_ms()
    require(plan["calibrationEndMs"] <= now <= plan["policyFreezeAtMs"]
            < plan["holdoutStartMs"], "NOT_BEFORE_UNTOUCHED_HOLDOUT")
    contexts = contexts_index(args.contexts)
    candidates, issues = [], Counter()
    for params in plan["parameterCandidates"]:
        rows, rejected = model_rows(source, contexts, params, plan, plan["trainingEndMs"])
        issues.update(rejected)
        candidates.append((params, rows))
    params, training = select_parameters(candidates, plan)
    require(len(training) >= plan["minimumTrainCases"], "INSUFFICIENT_GENUINE_TRAINING_HISTORY")
    model = fit_count_model(training, params, plan["contextColumns"])
    calibration_rows, rejected = model_rows(source, contexts, params, plan, plan["calibrationEndMs"])
    calibration_rows = [r for r in calibration_rows if plan["calibrationStartMs"] <= r["atMs"]]
    require(len(calibration_rows) >= plan["minimumCalibrationCases"], "NO_INDEPENDENT_DISTRIBUTION_CALIBRATION")
    distribution_calibration = [r for r in calibration_rows if r["atMs"] <= plan["calibrationFitEndMs"]
                                and r["labelAvailableAtMs"] <= plan["calibrationFitEndMs"]]
    require(len(distribution_calibration) >= plan["minimumCalibrationCases"], "INSUFFICIENT_CALIBRATION_FIT_CASES")
    model = calibrate_distribution(model, distribution_calibration, plan)
    # No future holdout price or outcome file is read during freeze.
    quotations = read_json(args.calibration_quotes)["quotes"]
    require(all(q["forecastAsOfMs"] <= plan["calibrationEndMs"] for q in quotations),
            "HOLDOUT_DATA_IN_CALIBRATION_PRICE_FILE")
    cases, rejected_quotes = priced_cases(source, contexts, quotations, params, plan,
                                          plan["calibrationPolicyStartMs"], plan["calibrationEndMs"])
    predictions = observed_predictions(model, cases, plan)
    require(all(c["labelAvailableAtMs"] <= plan["calibrationEndMs"] for c in predictions),
            "CALIBRATION_LABEL_NOT_AVAILABLE_BEFORE_FREEZE_PERIOD")
    bins = uncertainty_bins(predictions, plan)
    adverse_bins = uncertainty_bins([dict(c, central=c["adverseCentral"], numericalTail=c["adverseNumericalTail"])
                                    for c in predictions if c["historicalAdverseRealized"]], plan)
    policy = freeze_tiers(predictions, bins, adverse_bins, plan)
    save_new(args.output, {"contract": "statmaker-betting-v2-frozen-model-v1",
             "plan": plan, "planSha256": digest(plan), "model": model, "bins": bins, "policy": policy,
             "adverseBins": adverse_bins, "frozenAtMs": now, "trainingRows": training, "calibration": predictions,
             "sourceSha256": file_digest(args.source), "contextSha256": file_digest(args.contexts),
             "rejected": dict(issues+rejected+rejected_quotes)})


def evaluate(args) -> None:
    frozen = read_json(args.frozen)
    plan = frozen["plan"]
    verify_plan(plan)
    require(digest(plan) == frozen["planSha256"] and frozen["frozenAtMs"] < plan["holdoutStartMs"],
            "FROZEN_ARTIFACT_CHANGED_OR_LATE")
    now = now_ms()
    require(plan["holdoutEndMs"] <= now < plan["validUntilMs"], "HOLDOUT_NOT_COMPLETE_OR_STALE")
    source = input_source(args.source)
    # History may advance append-only after freeze; it cannot revise existing source observations.
    frozen_training = {r["apiFixtureId"]: r for r in frozen["trainingRows"]}
    available_history = {str(r["fixture_id"]): r for r in source["history"]}
    for row in frozen_training.values():
        for old in row["history"]+row["strengthHistory"]:
            current = available_history[str(old["fixture_id"])]
            require(old["betting_v2_provenance"] == current["betting_v2_provenance"],
                    "FROZEN_HISTORICAL_SOURCE_REVISED")
    model = frozen["model"]
    cases, rejected = priced_cases(source, contexts_index(args.contexts), read_json(args.holdout_quotes)["quotes"],
                                  model["parameters"], plan, plan["holdoutStartMs"], plan["holdoutEndMs"])
    predictions = observed_predictions(model, cases, dict(plan, evaluateAtMs=now))
    results = evaluate_holdout(predictions, frozen["calibration"], frozen["bins"], frozen["adverseBins"],
                               frozen["policy"], model, plan)
    documents = Documents()
    certificate = certify(model, plan, frozen["bins"], frozen["adverseBins"], frozen["policy"], results,
                          frozen["trainingRows"], documents, now, frozen["frozenAtMs"])
    save_new(args.output, {"contract": "statmaker-betting-v2-evaluated-model-v1",
             "certificate": certificate, "model": model, "plan": plan, "bins": frozen["bins"],
             "adverseBins": frozen["adverseBins"],
             "documents": documents.rows, "frozenArtifactSha256": file_digest(args.frozen),
             "holdoutPriceArtifactSha256": file_digest(args.holdout_quotes),
             "holdoutSourceSha256": file_digest(args.source), "holdoutResults": results,
             "rejected": dict(rejected)})


def produce(args) -> None:
    source, quotes = input_source(args.source), read_json(args.quotes)["quotes"]
    contexts = contexts_index(args.contexts)
    now = now_ms()
    models = {}
    family_models = {}
    for path in args.evaluated_models:
        assessed = read_json(path)
        require(assessed["contract"] == "statmaker-betting-v2-evaluated-model-v1", "NOT_EVALUATED_MODEL")
        certificate = assessed["certificate"]
        require(assessed["holdoutResults"]["unitStakeRoiLowerBound"] > 0, "NO_REAL_HOLDOUT_PROFITABILITY")
        scope = tuple(certificate[k] for k in ("competitionId", "leagueCode", "season", "marketModelKey", "evidenceMode"))
        require(scope not in models, "AMBIGUOUS_EVALUATED_MODEL")
        family_key = (certificate["competitionId"], certificate["leagueCode"],
                      certificate["season"], certificate["evidenceMode"], assessed["plan"]["family"])
        require(family_key not in family_models or family_models[family_key] == digest(assessed["model"]),
                "INCONSISTENT_RESULT_DC_DNB_COUNT_MODEL")
        family_models[family_key] = digest(assessed["model"])
        models[scope] = assessed
    documents = Documents()
    certs, forecasts, quote_rows, reasons = {}, [], [], Counter()
    prepared = defaultdict(list)
    # Index only by the SAME exact fields quote_for already checks. Indexing must
    # never select the best price or silently resolve ambiguous original quotes.
    quote_index = defaultdict(list)
    for quote in quotes:
        quote_index[(quote.get("selectionId"), quote.get("snapshotVersion"),
                     quote.get("generationId"))].append(quote)
    fixture_cache = {}
    feature_cache = {}
    card_history_cache = {}
    # Every raw quoted selection is attempted; no model/price shortlist drives discovery.
    for row in source["rawRows"]:
        try:
            require("blocker" not in row, row.get("blocker", "RAW_SOURCE_BLOCKED"))
            require(row["usableForStats"], "UNUSABLE_CANONICAL_FIXTURE")
            # Resolution and team-history features are matchup-specific, not
            # bookmaker-selection-specific. Reuse them across all offers of a
            # fixture while keeping every original offer and quote independent.
            fixture_key = (row["competitionId"], row["snapshotVersion"], row["fixtureId"],
                           row["leagueCode"], row["season"], row["kickoffEpochMs"],
                           row.get("canonicalHomeTeam") or row["homeTeam"],
                           row.get("canonicalAwayTeam") or row["awayTeam"])
            if fixture_key not in fixture_cache:
                fixture_cache[fixture_key] = resolve_fixture(row, source["schedule"])
            fixture = fixture_cache[fixture_key]
            # Model scope and matchup evidence must be assessed BEFORE asking
            # whether the bookmaker's current price makes the outcome valuable.
            # This also reports unsupported markets honestly, not as "bad odds".
            require(any((row["competitionId"], row["leagueCode"], row["season"],
                         row["marketModelKey"], mode) in models
                        for mode in ("XG_PRIMARY", "ELO_GOALS_FALLBACK_NO_XG")),
                    "NO_EVALUATED_MODEL_FOR_MARKET_SCOPE")
            context = contexts[str(fixture["fixture_id"])]
            found = []
            for mode in ("XG_PRIMARY", "ELO_GOALS_FALLBACK_NO_XG"):
                scope = (row["competitionId"], row["leagueCode"], row["season"], row["marketModelKey"], mode)
                assessed = models.get(scope)
                if assessed is None:
                    continue
                try:
                    history = source["history"]
                    if assessed["plan"]["family"] == "CARDS":
                        contract = assessed["plan"]["cardCountContract"]
                        contract_sha = digest(contract)
                        # A card model uses the provider's frozen counting
                        # contract. The bookmaker binding is checked only once
                        # the independent matchup forecast has been evaluated.
                        if contract_sha not in card_history_cache:
                            card_history_cache[contract_sha] = card_history(history, contract)
                        history = card_history_cache[contract_sha]
                    # A single assessed model has one frozen feature schema/plan.
                    # Never mix cases from different model certificates or fixtures.
                    feature_key = (str(fixture["fixture_id"]), id(assessed))
                    if feature_key not in feature_cache:
                        try:
                            feature_cache[feature_key] = (
                                "ok", case_for(history, fixture, context, {},
                                               assessed["model"]["parameters"],
                                               assessed["plan"], now))
                        except Blocked as error:
                            feature_cache[feature_key] = ("blocked", str(error))
                    status, template = feature_cache[feature_key]
                    if status == "blocked":
                        raise Blocked(template)
                    case = dict(template, offer=row)
                    found.append((assessed, case))
                except Blocked as error:
                    reasons[mode+":"+str(error)] += 1
                    continue
            require(len(found) == 1, "NO_UNAMBIGUOUS_INDEPENDENTLY_EVALUATED_EVIDENCE_MODE")
            assessed, case = found[0]
            q = quote_for(row, quote_index.get((row["selectionId"], row["snapshotVersion"],
                                               row["generationId"]), []), now)
            if assessed["plan"]["family"] == "CARDS":
                contract = assessed["plan"]["cardCountContract"]
                require(q["bookmaker"] == contract["bookmaker"]
                        and q["cardCountContractSha256"] == digest(contract),
                        "CARD_SETTLEMENT_MISMATCH")
            prepared[(row["competitionId"], row["fixtureId"], digest(assessed["model"]))].append((row, q, assessed, case))
        except (Blocked, KeyError, TypeError) as error:
            reasons[str(error)] += 1
    for group in prepared.values():
        model, plan = group[0][2]["model"], group[0][2]["plan"]
        cells = distribution(model, group[0][3]["features"], plan)
        groups = scenario_groups([v[0] for v in group], cells, plan["correlationThreshold"])
        for row, q, assessed, case in group:
            try:
                documents.rows.update(assessed["documents"])
                f = forecast(row, case, q, model, assessed["certificate"], assessed["bins"],
                             assessed["adverseBins"], plan, groups[row["selectionId"]], documents, now)
                certs[assessed["certificate"]["certificationId"]] = assessed["certificate"]
                forecasts.append({"competitionId": row["competitionId"], "snapshotVersion": row["snapshotVersion"],
                                  "fixtureId": row["fixtureId"], "selectionId": row["selectionId"], "payload": f})
                quote_rows.append({k: q[k] for k in ("generationId", "competitionId", "snapshotVersion",
                      "fixtureId", "selectionId", "bookmaker", "providerQuoteId", "observedAtMs", "validUntilMs", "odd")})
            except (Blocked, KeyError, TypeError) as error:
                reasons[str(error)] += 1
    save_new(args.output, {"contract": "statmaker-betting-v2-canonical-release-v1",
             "generationId": source["generationId"], "producedAtMs": now,
             "rawInventorySha256": source["unfilteredRawInventorySha256"],
             "rawInventoryRowCount": len(source["rawRows"]), "rawSelectionCount": source["rawSelectionCount"],
             "certificates": list(certs.values()),
             "forecasts": forecasts, "quotes": quote_rows, "documents": documents.rows,
             "rejected": dict(reasons), "notYetPublished": True})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="phase", required=True)
    p = commands.add_parser("inventory")
    p.add_argument("--repository-root", type=Path, required=True)
    p.add_argument("--prepared-db", type=Path, required=True)
    p.add_argument("--generation-id", required=True)
    p.add_argument("--workspace", type=Path, required=True)
    for name in ("quotes", "contexts", "freeze", "evaluate", "produce"):
        p = commands.add_parser(name)
        p.add_argument("--source", type=Path, required=True)
        if name not in ("quotes", "contexts"):
            p.add_argument("--contexts", type=Path, required=True)
        p.add_argument("--output", type=Path, required=True)
        if name == "quotes":
            p.add_argument("--receipts", type=Path, nargs="+", required=True)
            p.add_argument("--attestations", type=Path, required=True)
        elif name == "contexts":
            p.add_argument("--receipts", type=Path, nargs="+", required=True)
            p.add_argument("--maximum-age-ms", type=int, required=True)
        elif name == "freeze":
            p.add_argument("--plan", type=Path, required=True)
            p.add_argument("--calibration-quotes", type=Path, required=True)
        elif name == "evaluate":
            p.add_argument("--frozen", type=Path, required=True)
            p.add_argument("--holdout-quotes", type=Path, required=True)
        else:
            p.add_argument("--quotes", type=Path, required=True)
            p.add_argument("--evaluated-models", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    if args.phase != "inventory":
        # All generated artifacts live outside checked-in PROD/research data.
        require(not args.output.exists(), "IMMUTABLE_OUTPUT_ALREADY_EXISTS")
        parts = set(args.output.resolve().parts)
        require(not parts.intersection({"data", "reports", ".github"}), "PROTECTED_OUTPUT_PATH")
    try:
        globals()[args.phase](args)
    except Blocked as error:
        raise SystemExit("BLOCKED: " + str(error))


if __name__ == "__main__":
    main()
