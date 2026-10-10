#!/usr/bin/env python3
"""Append-only, zero-extra-request receipts from existing Odds-API.io /odds calls.

Captures the parsed provider RESPONSE exactly as delivered to the existing
refresh normalizer, before merge/rotation. The receipt UTC time is when the
CLIENT received a response; it is NOT a provider price-update timestamp.
Full decoded payload is retained for later exact offer/fixture/market audits.

No extra HTTP calls, no changes to provider responses or API quota. Receipts
are research evidence, not independent quote certificates or STRONG decisions.
"""
from __future__ import annotations

import datetime as dt
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Callable

_RECEIPT_MARKER = "_statmaker_betting_v2_quote_receipt_installed"
MAX_DECODED_BYTES = 45_000_000
MAX_RECEIPTS_PER_PROCESS = 100
STATUS_PATH = "reports/betting_v2/quote_receipts"


def canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def receipt_document(
    payload: Any, path: str, params: dict[str, Any],
    received_at: dt.datetime, *, max_decoded_bytes: int = MAX_DECODED_BYTES,
) -> dict[str, Any]:
    """Hash the actual decoded provider response and keep safe request context.

    There is NO per-selection timestamp assertion, identity crosswalk or
    unbiased market-universe assertion. No API keys are ever serialized.
    """
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("A timezone-aware client receipt instant is required")
    if path not in ("/odds", "/odds/multi"):
        raise ValueError("Receipts apply only to real provider odds responses")
    wire = canonical(payload)
    digest = sha256(wire).hexdigest()
    safe_request = {
        "bookmakers": str(params.get("bookmakers") or ""),
        "eventId": str(params.get("eventId") or ""),
        "eventIds": str(params.get("eventIds") or ""),
    }
    safe_request = {k: v for k, v in safe_request.items() if v}
    if not safe_request.get("eventId") and not safe_request.get("eventIds"):
        raise ValueError("Missing provider event identity in odds request")
    stored = len(wire) <= max_decoded_bytes
    return {
        "contract": "betting-v2-client-odds-http-receipt-v1",
        "provider": "Odds-API.io",
        "endpoint": path,
        "request": safe_request,
        "clientReceivedAtUTC": received_at.astimezone(
            dt.timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "decodedResponseSha256": digest,
        "decodedResponseBytes": len(wire),
        "fullDecodedResponseStored": stored,
        "decodedResponse": payload if stored else None,
        "timestampSemantics": "CLIENT_RESPONSE_RECEIVED_AT_NOT_PROVIDER_LAST_PRICE_UPDATE",
        "priceObservationTimestampVerified": False,
        "bookmakerMarketSelectionIdVerified": False,
        "independentUnfilteredBookmakerUniverseVerified": False,
        "certifiedStrong": False,
        "researchOnly": True,
        "extraProviderRequests": 0,
    }



def verify_receipt(doc: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed authenticity/integrity check for a captured decoded reply.

    Hash + client time establish local receipt integrity only. The verifier
    cannot establish provider quote-update time, independent market coverage,
    bookmaker selection identity or profitable expected value.
    """
    if doc.get("contract") != "betting-v2-client-odds-http-receipt-v1":
        raise ValueError("Unknown quote receipt contract")
    if doc.get("researchOnly") is not True or doc.get("certifiedStrong") is not False:
        raise ValueError("A quote receipt must not claim betting certification")
    if doc.get("endpoint") not in ("/odds", "/odds/multi"):
        raise ValueError("Unexpected endpoint")
    req = doc.get("request")
    if not isinstance(req, dict) or (
        not req.get("eventId") and not req.get("eventIds")
    ) or "apiKey" in req:
        raise ValueError("Missing event identity or secret in request")
    from quote_provenance import parse_utc
    observed = parse_utc(doc.get("clientReceivedAtUTC"))
    if observed is None:
        raise ValueError("Client receipt must be timezone aware")
    if doc.get("fullDecodedResponseStored") is not True:
        raise ValueError("Incomplete received payload cannot prove offer identity")
    payload = doc.get("decodedResponse")
    if not isinstance(payload, (dict, list)):
        raise ValueError("Provider response not a structured JSON value")
    wire = canonical(payload)
    if len(wire) != doc.get("decodedResponseBytes"):
        raise ValueError("Received payload byte length differs")
    if sha256(wire).hexdigest() != doc.get("decodedResponseSha256"):
        raise ValueError("Received payload SHA-256 differs")
    if (doc.get("priceObservationTimestampVerified") is not False or
            doc.get("independentUnfilteredBookmakerUniverseVerified") is not False or
            doc.get("bookmakerMarketSelectionIdVerified") is not False):
        raise ValueError("Unverified individual quote cannot claim provenance")
    return {
        "verifiedLocalReceiptIntegrity": True,
        "clientReceivedAtUTC": doc["clientReceivedAtUTC"],
        "sourceGenerationId": doc["decodedResponseSha256"],
        "providerOfferTimestampIndependentlyVerified": False,
        "independentBookmakerUniverseVerified": False,
        "readyForCertifyingStrong": False,
    }


def write_receipt(
    root: Path, payload: Any, path: str, params: dict[str, Any],
    received_at: dt.datetime, *,
    max_decoded_bytes: int = MAX_DECODED_BYTES,
) -> Path:
    receipt = receipt_document(
        payload, path, params, received_at,
        max_decoded_bytes=max_decoded_bytes,
    )
    root = root.resolve()
    directory = (root / STATUS_PATH).resolve()
    directory.relative_to(root)
    directory.mkdir(parents=True, exist_ok=True)
    instant = received_at.astimezone(dt.timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    stem = f"{instant}_{receipt['decodedResponseSha256'][:16]}"
    # O_EXCL prevents silent modification of a previously captured receipt.
    for attempt in range(5):
        candidate = directory / f"{stem}_{attempt}.json.gz"
        try:
            with candidate.open("xb") as stream:
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=stream, compresslevel=7,
                    mtime=0,
                ) as zip_stream:
                    zip_stream.write(canonical(receipt))
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError("Could not allocate append-only receipt name")


def install(
    odds_module: Any, root: Path, *,
    now_fn: Callable[[], dt.datetime] | None = None,
    enabled: bool = True,
) -> None:
    """Hook existing odds HTTP calls, preserving return objects and exceptions."""
    if not enabled or getattr(odds_module, _RECEIPT_MARKER, False):
        return
    now = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))
    original = odds_module.api_get
    count = [0]

    def captured(path: str, params: dict[str, Any], debug: dict,
                 *, allow_error: bool = True) -> Any:
        result = original(path, params, debug, allow_error=allow_error)
        if path not in ("/odds", "/odds/multi") or result is None:
            return result
        if not isinstance(result, (dict, list)):
            return result
        if count[0] >= MAX_RECEIPTS_PER_PROCESS:
            debug.setdefault("bettingV2ReceiptWarnings", []).append(
                "receipt_count_cap_reached"
            )
            return result
        count[0] += 1
        try:
            dest = write_receipt(root, result, path, params, now())
            debug.setdefault("bettingV2ReceiptPaths", []).append(
                dest.relative_to(root.resolve()).as_posix()
            )
        except (OSError, ValueError, TypeError, OverflowError) as error:
            # Research receipts must NEVER break core live odds publication.
            debug.setdefault("bettingV2ReceiptWarnings", []).append(
                type(error).__name__
            )
        return result

    odds_module.api_get = captured
    setattr(odds_module, _RECEIPT_MARKER, True)
