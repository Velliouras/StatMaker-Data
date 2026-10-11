"""Actual cached context receipts and grounded doubtful-player adverse scenarios.

Capture is opt-in at an existing successful response, with no new HTTP requests.
Historical adverse labels require later, actual confirmed starting XIs; the
realization record never becomes a feature that was known at the earlier as-of.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .production_contract import Blocked, digest, milliseconds, require, save_new

CONTRACT = "statmaker-betting-v2-context-receipt-v1"


def capture_context_response(directory: Path, endpoint: str, parameters: dict, items: list) -> None:
    require(endpoint in ("/injuries", "/fixtures"), "UNSUPPORTED_CONTEXT_RECEIPT")
    observed = int(datetime.now(timezone.utc).timestamp()*1000)
    receipt = {"contract": CONTRACT, "provider": "api-football", "endpoint": endpoint,
               "parameters": parameters, "clientReceivedAtMs": observed,
               "response": items, "responseSha256": digest(items)}
    path = directory.resolve()
    require(not set(path.parts).intersection({"data", "reports", ".github"}), "PROTECTED_CONTEXT_ARCHIVE")
    save_new(path/(str(observed)+"-"+digest(receipt)+".json"), receipt)


def verify_receipt(receipt: dict) -> None:
    require(receipt.get("contract") == CONTRACT and receipt.get("provider") == "api-football"
            and digest(receipt["response"]) == receipt["responseSha256"], "INVALID_CONTEXT_RECEIPT")
    require(milliseconds(receipt["clientReceivedAtMs"]) > 0, "MISSING_ACTUAL_CONTEXT_RECEIPT_TIME")


def bound_value(record: dict, binding: dict) -> float:
    require(binding["operation"] == "PROVIDER_PLAYER_COUNT", "UNKNOWN_CONTEXT_TRANSFORM")
    verify_receipt(record["receipt"])
    team = str(binding["teamId"])
    statuses = set(binding["types"])
    players = set()
    for item in record["receipt"]["response"]:
        if str(item["fixture"]["id"]) != str(record["fixtureId"]):
            continue
        if str(item["team"]["id"]) == team and item["player"]["type"] in statuses:
            require(item["player"].get("id"), "MISSING_INJURED_PLAYER_ID")
            players.add(str(item["player"]["id"]))
    return float(len(players))


def adapt_context_receipts(source: dict, receipts: list[dict], maximum_age_ms: int) -> tuple[list[dict], Counter]:
    require(maximum_age_ms > 0, "NO_CONTEXT_FRESHNESS_POLICY")
    targets = {}
    for item in source["schedule"]+source["history"]:
        key = str(item["fixture_id"])
        require(key not in targets or targets[key] == item, "AMBIGUOUS_CONTEXT_FIXTURE_SOURCE")
        targets[key] = item
    valid, issues = [], Counter()
    for receipt in receipts:
        try:
            verify_receipt(receipt)
            valid.append(receipt)
        except (Blocked, KeyError, TypeError) as error:
            issues[str(error)] += 1
    result = []
    for key, target in targets.items():
        try:
            injury = [r for r in valid if r["endpoint"] == "/injuries"
                      and key in str(r["parameters"].get("ids", "")).split("-")
                      and r["clientReceivedAtMs"] < target["kickoffEpochMs"]]
            require(bool(injury), "NO_PREMATCH_INJURY_RECEIPT")
            # Predeclared first actual observation; never choose a receipt by outcome.
            receipt = min(injury, key=lambda r: r["clientReceivedAtMs"])
            home, away = str(target["home_team_id"]), str(target["away_team_id"])
            relevant = [i for i in receipt["response"] if str(i["fixture"]["id"]) == key]
            require(all(str(i["team"]["id"]) in (home, away) for i in relevant), "INJURY_TEAM_MISJOIN")
            require(all(i["player"].get("id") and i["player"].get("type") in
                        ("Missing Fixture", "Questionable") for i in relevant), "UNKNOWN_INJURY_STATUS")
            doubtful = {str(i["player"]["id"]) for i in relevant if i["player"]["type"] == "Questionable"}
            require(bool(doubtful), "NO_OBSERVABLE_ADVERSE_PLAYER_SCENARIO")
            record = {"fixtureId": key, "homeTeamId": home, "awayTeamId": away, "receipt": receipt}
            observed = receipt["clientReceivedAtMs"]
            expires = min(target["kickoffEpochMs"], observed+maximum_age_ms)
            def state(statuses, kind):
                bindings = {name: {"operation": "PROVIDER_PLAYER_COUNT", "teamId": team, "types": statuses}
                            for name, team in (("homeUnavailablePlayers", home), ("awayUnavailablePlayers", away))}
                return {"fixtureId": key, "scenarioKind": kind, "observedAtMs": observed, "validUntilMs": expires,
                        "providerRecord": record, "providerRecordSha256": digest(record),
                        "fieldBindings": bindings,
                        "values": {name: bound_value(record, binding) for name, binding in bindings.items()}}
            base = state(["Missing Fixture"], "BASE")
            adverse = state(["Missing Fixture", "Questionable"], "ADVERSE_DOUBTFUL")
            adverse["realisticAdverseInputs"] = [{"playerId": p, "scenario": "provider-doubtful-player-unavailable"}
                                                 for p in sorted(doubtful)]
            adverse["scenarioId"] = digest({"fixtureId": key, "doubtfulPlayers": sorted(doubtful),
                                            "source": digest(receipt)})
            proof = []
            for candidate in valid:
                if candidate["endpoint"] != "/fixtures" or not observed <= candidate["clientReceivedAtMs"] < target["kickoffEpochMs"]:
                    continue
                for fixture in candidate["response"]:
                    if str(fixture["fixture"]["id"]) != key:
                        continue
                    lineups = {str(b["team"]["id"]): b.get("startXI", []) for b in fixture.get("lineups", [])}
                    if home not in lineups or away not in lineups or any(len(lineups[t]) != 11 for t in (home, away)):
                        continue
                    starters = {str(p["player"]["id"]) for t in (home, away) for p in lineups[t]}
                    if not doubtful.intersection(starters):
                        proof.append(candidate)
            adverse["historicallyRealized"] = bool(proof)
            if proof:
                realization = min(proof, key=lambda r: r["clientReceivedAtMs"])
                adverse["realizationReceipt"] = realization
                adverse["realizationReceiptSha256"] = digest(realization)
            result.append({"fixtureId": key, "providerRecord": record,
                           "providerRecordSha256": digest(record), "base": base, "adverse": adverse})
        except (Blocked, KeyError, TypeError) as error:
            issues[str(error)] += 1
    return result, issues
