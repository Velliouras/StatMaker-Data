"""Adapt existing cached quote receipts, without a fetch or inferred price timestamp.

Receipts alone are NOT attestations. Independent reviewed evidence is required for
provider IDs, timestamp semantics, complete returned universe and price partitions.
No script here promotes clientReceivedAtUTC to provider observation/expiration time.
"""
from __future__ import annotations

from collections import Counter

from .production_contract import Blocked, digest, milliseconds, number, require
from .production_sources import market_identity, quote_for, resolve_fixture
from .quote_receipt_capture import verify_receipt


def adapt_quote_receipts(source: dict, receipts: list[dict], attestations: dict, now: int) -> tuple[list[dict], Counter]:
    offers, issues = [], Counter()
    inventory = {}
    for row in source["rawRows"]:
        if "selectionId" in row and "blocker" not in row:
            inventory.setdefault((row["fixtureId"], row["market"], row["selection"],
                                  row.get("team"), row.get("line"), row["odd"]), []).append(row)
    for receipt in receipts:
        try:
            status = verify_receipt(receipt)
            receipt_sha = digest(receipt)
            attested = attestations[receipt_sha]
            require(attested["receiptSha256"] == receipt_sha
                    and attested["independentUnfilteredBookmakerUniverseVerified"] is True
                    and attested["priceObservationTimestampVerified"] is True,
                    "CLIENT_RECEIPT_NOT_PROVIDER_PRICE_ATTESTATION")
            require(digest(attested["reviewEvidence"]) == attested["reviewEvidenceSha256"]
                    and attested["reviewEvidence"]["receiptSha256"] == receipt_sha,
                    "NO_INDEPENDENT_RECEIPT_REVIEW_EVIDENCE")
            received = milliseconds(status["clientReceivedAtUTC"])
            for event in receipt["boundedMarketSnapshot"]:
                event_id = str(event["event"].get("id") or event["event"].get("eventId") or "")
                for book in event["bookmakers"]:
                    for market in book["markets"]:
                        for outcome in market["outcomes"]:
                            try:
                                # Exact mappings refer to the original received object, never fuzzy matching.
                                source_sha = digest({"event": event["event"], "bookmaker": book["bookmaker"],
                                                     "market": market, "outcome": outcome})
                                mapping = attested["quoteMappings"][source_sha]
                                raw = mapping["providerRecord"]
                                require(raw["id"] == str(outcome["id"]) and raw["eventId"] == event_id
                                        and raw["bookmaker"] == book["bookmaker"], "QUOTE_ID_MAPPING_NOT_ORIGINAL")
                                require(milliseconds(raw["observedAt"]) == milliseconds(outcome["observedAt"])
                                        and milliseconds(raw["validUntil"]) == milliseconds(outcome["validUntil"])
                                        and raw["odd"] == number(outcome["decimalOdds"]), "MISSING_PROVIDER_PRICE_FIELDS")
                                identity = mapping["providerMarketIdentity"]
                                key = (event_id, identity["market"], identity["selection"],
                                       identity.get("team"), identity.get("line"), raw["odd"])
                                matches = inventory.get(key, [])
                                require(len(matches) == 1, "NO_EXACT_CANONICAL_RAW_QUOTE")
                                row = market_identity(matches[0])
                                fixture = resolve_fixture(row, source["schedule"]+source["history"])
                                q = {
                                    **{k: row[k] for k in ("generationId", "competitionId", "snapshotVersion",
                                        "fixtureId", "selectionId", "odd")},
                                    "providerRecord": raw, "providerRecordSha256": digest(raw),
                                    "providerEventId": event_id, "providerQuoteId": raw["id"],
                                    "bookmaker": raw["bookmaker"], "observedAtMs": milliseconds(raw["observedAt"]),
                                    "validUntilMs": milliseconds(raw["validUntil"]), "clientReceivedAtMs": received,
                                    "providerMarketIdentity": identity,
                                    "independentUnfilteredBookmakerUniverseVerified": True,
                                    "priceObservationTimestampVerified": True, "canonicalOffer": row,
                                    "apiFootballFixtureId": str(fixture["fixture_id"]),
                                    "homeTeamId": str(fixture["home_team_id"]), "awayTeamId": str(fixture["away_team_id"]),
                                    "unfilteredUniverse": attested["bookmakerUniverses"][book["bookmaker"]],
                                    "comparisonProviderQuoteIds": mapping["comparisonProviderQuoteIds"],
                                    "comparisonPartitionVerified": True,
                                    "sourceReceiptSha256": receipt_sha,
                                    "reviewEvidenceSha256": attested["reviewEvidenceSha256"],
                                    # Retrospective evaluation as-of, NOT an actual historical issued pick.
                                    "forecastAsOfMs": received,
                                }
                                q["unfilteredUniverseSha256"] = digest(q["unfilteredUniverse"])
                                if "cardCountContractSha256" in mapping:
                                    q["cardCountContractSha256"] = mapping["cardCountContractSha256"]
                                require(received <= now, "FUTURE_CLIENT_RECEIPT")
                                offers.append(quote_for(row, [q], received))
                            except (Blocked, KeyError, TypeError) as error:
                                issues[str(error)] += 1
        except (Blocked, KeyError, TypeError, ValueError) as error:
            issues[str(error)] += 1
    return offers, issues
