#!/usr/bin/env python3
"""Append-only, zero-extra-request receipts from existing Odds-API.io /odds calls.

Observes the parsed provider response and stores only bounded, whitelisted
event/bookmaker/market/outcome evidence, before merge/rotation. The receipt UTC time is when the
CLIENT received a response; it is NOT a provider price-update timestamp.
Raw full provider responses are NOT published in the public Data repository.
A source SHA-256 allows later comparison against the original provider response
if legally and securely retained elsewhere; it does not prove quote freshness.

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



# Public Data/main must not become an indefinite mirror of raw provider payloads.
# Retain bounded market/outcome evidence only, not unrelated response metadata.
MAX_PUBLIC_SNAPSHOT_BYTES = 256_000
_MARKET_FIELDS = frozenset((
    "id", "key", "name", "market", "type", "line", "hdp", "updatedAt",
    "updated_at", "lastUpdatedAt", "lastUpdate", "odds", "outcomes", "prices",
))
_OUTCOME_FIELDS = frozenset((
    "id", "key", "name", "label", "selection", "team", "side", "line", "hdp",
    "odds", "price", "decimal", "decimalOdds", "value", "over", "under",
    "home", "away", "draw", "yes", "no", "1X", "X2", "12",
))
_EVENT_FIELDS = frozenset((
    "id", "eventId", "date", "kickoff", "startTime", "home", "away",
    "homeTeam", "awayTeam", "league",
))


def _safe_scalar(v: Any) -> str | float | int | bool | None:
    if v is None or isinstance(v, (int, float, bool)):
        return v
    if isinstance(v, str):
        return v[:160]
    return None


def _projection_row(row: Any, fields: frozenset[str]) -> dict:
    if not isinstance(row, dict):
        return {}
    out = {}
    for key, value in row.items():
        if key not in fields:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = _safe_scalar(value)
    return out


def projected_market_snapshot(payload: Any, event_context: dict[str, dict] | None = None) -> tuple[list[dict], bool]:
    """Capture supported field shapes from every returned bookmaker market.

    Incomplete or unusual provider shapes are flagged, never silently
    represented as a verified full offer universe.
    """
    events = payload if isinstance(payload, list) else [payload]
    if not isinstance(events, list) or len(events) > 300:
        return [], False
    snapshots = []
    complete = True
    for event in events:
        if not isinstance(event, dict):
            complete = False
            continue
        context = _projection_row(event, _EVENT_FIELDS)
        eid = context.get("id") or context.get("eventId")
        if eid is None:
            complete = False
        else:
            # The existing /events request precedes /odds in the same cycle.
            # Only attach that provider event's *own* exact event ID metadata.
            from_events = (event_context or {}).get(str(eid), {})
            for field in ("date", "kickoff", "startTime", "home", "away",
                          "homeTeam", "awayTeam"):
                if not context.get(field) and from_events.get(field):
                    context[field] = from_events[field]
        raw = event.get("bookmakers") or event.get("odds")
        if isinstance(raw, dict):
            books = [{
                "name": key,
                "markets": (value.get("markets") or value.get("odds"))
                if isinstance(value, dict) else value,
            } for key, value in raw.items()]
        elif isinstance(raw, list):
            books = raw
        else:
            books = []
            complete = False
        if len(books) > 80:
            complete = False
            books = books[:80]
        saved_books = []
        for book in books:
            if not isinstance(book, dict):
                complete = False
                continue
            name = str(book.get("name") or book.get("bookmaker") or
                       book.get("key") or book.get("title") or "")
            markets = book.get("markets") or book.get("odds")
            if isinstance(markets, dict):
                markets = [{"name": key, "odds": value} for key, value in markets.items()]
            if not name or not isinstance(markets, list):
                complete = False
                continue
            if len(markets) > 300:
                complete = False
            saved_markets = []
            for market in markets[:300]:
                if not isinstance(market, dict):
                    complete = False
                    continue
                item = _projection_row(market, _MARKET_FIELDS)
                values = market.get("outcomes") or market.get("odds") or market.get("prices")
                if isinstance(values, dict):
                    values = [{"name": k, **v} if isinstance(v, dict)
                              else {"name": k, "odds": v}
                              for k, v in values.items()]
                if not isinstance(values, list):
                    complete = False
                    continue
                if len(values) > 150:
                    complete = False
                item["outcomes"] = [
                    _projection_row(row, _OUTCOME_FIELDS)
                    for row in values[:150] if isinstance(row, dict)
                ]
                if len(item["outcomes"]) != min(len(values), 150):
                    complete = False
                saved_markets.append(item)
            saved_books.append({"bookmaker": name[:120], "markets": saved_markets})
        snapshots.append({"event": context, "bookmakers": saved_books})
    return snapshots, complete


def receipt_document(
    payload: Any, path: str, params: dict[str, Any],
    received_at: dt.datetime, *, max_decoded_bytes: int = MAX_DECODED_BYTES,
    event_context: dict[str, dict] | None = None,
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
    projected, complete = projected_market_snapshot(payload, event_context)
    snapshot_wire = canonical(projected)
    stored = (len(wire) <= max_decoded_bytes and
              len(snapshot_wire) <= MAX_PUBLIC_SNAPSHOT_BYTES)
    return {
        "contract": "betting-v2-client-odds-http-receipt-v2",
        "provider": "Odds-API.io",
        "endpoint": path,
        "request": safe_request,
        "clientReceivedAtUTC": received_at.astimezone(
            dt.timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "decodedResponseSha256": digest,
        "decodedResponseBytes": len(wire),
        "fullDecodedResponseStored": False,
        "rawProviderBodyPublished": False,
        "boundedMarketSnapshot": projected if stored else [],
        "marketSnapshotSha256": sha256(snapshot_wire).hexdigest() if stored else None,
        "marketSnapshotComplete": bool(stored and complete),
        "marketSnapshotBytes": len(snapshot_wire) if stored else 0,
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
    if doc.get("contract") != "betting-v2-client-odds-http-receipt-v2":
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
    raw_observed = doc.get("clientReceivedAtUTC")
    if not isinstance(raw_observed, str):
        raise ValueError("Client receipt timestamp missing")
    try:
        observed = dt.datetime.fromisoformat(
            raw_observed.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("Malformed client receipt timestamp") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Client receipt must be timezone aware")
    if doc.get("fullDecodedResponseStored") is not False or (
        doc.get("rawProviderBodyPublished") is not False
    ):
        raise ValueError("Raw provider response must not be published in public Git")
    snapshot = doc.get("boundedMarketSnapshot")
    if not isinstance(snapshot, list) or not doc.get("marketSnapshotComplete"):
        raise ValueError("Incomplete offer snapshot cannot prove price evidence")
    wire = canonical(snapshot)
    if len(wire) != doc.get("marketSnapshotBytes"):
        raise ValueError("Stored offer snapshot byte count mismatch")
    if sha256(wire).hexdigest() != doc.get("marketSnapshotSha256"):
        raise ValueError("Stored offer snapshot integrity mismatch")
    if (doc.get("priceObservationTimestampVerified") is not False or
            doc.get("independentUnfilteredBookmakerUniverseVerified") is not False or
            doc.get("bookmakerMarketSelectionIdVerified") is not False):
        raise ValueError("Unverified individual quote cannot claim provenance")
    return {
        "verifiedLocalReceiptIntegrity": True,
        "marketSnapshotComplete": True,
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
    event_context: dict[str, dict] | None = None,
) -> Path:
    receipt = receipt_document(
        payload, path, params, received_at,
        max_decoded_bytes=max_decoded_bytes,
        event_context=event_context,
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
    events_seen: dict[str, dict] = {}

    def captured(path: str, params: dict[str, Any], debug: dict,
                 *, allow_error: bool = True) -> Any:
        result = original(path, params, debug, allow_error=allow_error)
        if path == "/events":
            rows = result if isinstance(result, list) else [result]
            for event in rows:
                if isinstance(event, dict):
                    identity = event.get("id") or event.get("eventId")
                    if identity is not None:
                        events_seen[str(identity)] = _projection_row(
                            event, _EVENT_FIELDS
                        )
            # Bound in-memory context even for unusually large global /events.
            if len(events_seen) > 12_000:
                events_seen.clear()
            return result
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
            dest = write_receipt(
                root, result, path, params, now(), event_context=events_seen
            )
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
