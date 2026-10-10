#!/usr/bin/env python3
"""Read-only, no-provider-call index of Betting V2 bounded odds receipts.

Produces a countable, reproducible list of observed bookmaker MARKET offers.
No cross-provider fixture identity is asserted; no STRONG, ROI or EV produced.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import json
from math import isfinite
from pathlib import Path
from typing import Any

from quote_receipt_capture import STATUS_PATH, verify_receipt

MAX_GZIP_FILE_BYTES = 350_000
MAX_RECEIPT_FILES = 750
PRICE_FIELDS = ("odds", "price", "decimal", "decimalOdds", "value")
DIRECTION_FIELDS = ("home", "draw", "away", "over", "under", "yes", "no", "1X", "X2", "12")


def _price(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        price = float(v)
    except (ValueError, TypeError):
        return None
    return price if isfinite(price) and price > 1.0 else None


def extracted_offers(receipt: dict) -> tuple[list[dict], Counter]:
    """Flatten without guessing bookmaker event-to-API-Football crosswalks."""
    status = verify_receipt(receipt)
    observed = datetime.fromisoformat(
        status["clientReceivedAtUTC"].replace("Z", "+00:00")
    )
    offers = []
    issues = Counter()
    for item in receipt["boundedMarketSnapshot"]:
        event = item["event"]
        event_id = str(event.get("id") or event.get("eventId") or "")
        raw_kickoff = event.get("date") or event.get("kickoff") or event.get("startTime")
        try:
            kickoff = datetime.fromisoformat(str(raw_kickoff).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            kickoff = None
        if not event_id or kickoff is None or kickoff.tzinfo is None:
            issues["missing_event_or_absolute_kickoff"] += 1
            continue
        if observed >= kickoff.astimezone(timezone.utc):
            issues["received_at_or_after_kickoff"] += 1
            continue
        for book in item["bookmakers"]:
            bookmaker = str(book.get("bookmaker") or "").strip()
            if not bookmaker:
                issues["missing_bookmaker"] += 1
                continue
            for market in book.get("markets") or []:
                name = str(market.get("name") or market.get("market") or
                           market.get("type") or market.get("key") or "").strip()
                if not name:
                    issues["missing_market_name"] += 1
                    continue
                for row in market.get("outcomes") or []:
                    line = row.get("hdp") if row.get("hdp") is not None else (
                        row.get("line"))
                    row_name = str(row.get("name") or row.get("label") or
                                   row.get("selection") or "").strip()
                    entries = []
                    for field in PRICE_FIELDS:
                        p = _price(row.get(field))
                        if p is not None:
                            entries.append((row_name or field, p))
                            break
                    for field in DIRECTION_FIELDS:
                        p = _price(row.get(field))
                        if p is not None:
                            entries.append((field.upper(), p))
                    if not entries:
                        issues["outcome_without_numeric_price"] += 1
                    for selection, p in entries:
                        offers.append({
                            "providerEventId": event_id,
                            "kickoffUTC": kickoff.astimezone(timezone.utc).isoformat(),
                            "bookmaker": bookmaker,
                            "providerMarketName": name,
                            "selection": selection,
                            "providerSelectionId": str(row.get("id") or row.get("key") or ""),
                            "line": line,
                            "odd": p,
                            "clientReceivedAtUTC": status["clientReceivedAtUTC"],
                            "sourceGenerationId": status["sourceGenerationId"],
                            "priceObservationTimestampVerified": False,
                            "independentUnfilteredBookmakerUniverseVerified": False,
                            "exactApiFootballFixtureMatchVerified": False,
                            "certifiedStrong": False,
                        })
    return offers, issues


def inspect(root: Path, *, max_files: int = MAX_RECEIPT_FILES) -> dict:
    if max_files < 1:
        raise ValueError("max_files must be positive")
    directory = root.resolve() / STATUS_PATH
    files = sorted(directory.glob("*.json.gz")) if directory.exists() else []
    truncated = len(files) > max_files
    if truncated:
        # This is a recent-receipts diagnostic, not a full archive claim.
        files = files[-max_files:]
    counters: Counter = Counter()
    by_market = Counter()
    by_bookmaker = Counter()
    errors: Counter = Counter()
    fixture_ids: set[str] = set()
    for path in files:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_GZIP_FILE_BYTES:
            errors["unsafe_or_oversized_receipt"] += 1
            continue
        try:
            # GZIP decompression is separately capped to 1 MiB.
            with gzip.open(path, "rb") as f:
                raw = f.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("Expanded receipt too large")
            doc = json.loads(raw)
            offers, issues = extracted_offers(doc)
        except (OSError, EOFError, ValueError, TypeError, KeyError):
            errors["invalid_receipt"] += 1
            continue
        counters["verifiedSnapshotReceipts"] += 1
        counters["preKickoffPriceRows"] += len(offers)
        errors.update(issues)
        for offer in offers:
            by_market[offer["providerMarketName"]] += 1
            by_bookmaker[offer["bookmaker"]] += 1
            fixture_ids.add(offer["providerEventId"])
    return {
        "contract": "betting-v2-client-quote-index-v1",
        "researchOnly": True,
        "certifiedStrong": 0,
        "hasPricedProfitCertificate": False,
        "providerCalls": 0,
        "recordsScanned": len(files),
        "olderReceiptFilesOmittedByLimit": truncated,
        "maxFilesScanned": max_files,
        "verifiedSnapshotReceipts": counters["verifiedSnapshotReceipts"],
        "preKickoffPriceRows": counters["preKickoffPriceRows"],
        "uniqueProviderEvents": len(fixture_ids),
        "byMarket": dict(sorted(by_market.items())),
        "byBookmaker": dict(sorted(by_bookmaker.items())),
        "rejected": dict(sorted(errors.items())),
        "warning": "Client-observed price rows require independent provider identity, time and full-universe validation plus fixture crosswalk before use as V2 STRONG evidence.",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--report", type=Path, default=Path("reports/betting_v2/quote_receipt_index.json"))
    args = ap.parse_args()
    root = args.repository_root.resolve()
    report = inspect(root)
    target = (root / args.report).resolve()
    if not target.is_relative_to(root / "reports/betting_v2"):
        raise ValueError("Audit outputs must remain in research reports")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    tmp.replace(target)
    print(json.dumps({
        "receipts": report["recordsScanned"],
        "preKickoffPriceRows": report["preKickoffPriceRows"],
        "providerCalls": 0,
        "certifiedStrong": 0,
    }))


if __name__ == "__main__":
    main()
