#!/usr/bin/env python3
"""Read-only local odds source structure/provenance audit; NO API requests.

Never promote an archive-generation time into a per-bookmaker quote timestamp.
This exploratory shape audit does not assert verified independent quote identity,
market coverage, unfiltered selection universe, EV, ROI, or STRONG.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

SOURCE = "odds/odds_api_io/domestic_odds.json"
SOURCE_MAX_BYTES = 110_000_000
NODES_MAX = 400_000
TIMESTAMP_KEYS = frozenset({
    "quoteobservedat", "observedat", "bookmakerupdatedat",
    "bookmakerlastupdate", "sourceobservedat", "lastupdate",
    "updatedat", "timestamp", "lastupdated", "lastupdateutc",
})
PRICE_KEYS = frozenset({"odd", "odds", "price", "decimalodds", "decimalprice"})
BOOKMAKER_KEYS = frozenset({"bookmaker", "bookmakerid", "bookmakerkey", "bookmakername"})
SELECTION_KEYS = frozenset({
    "selectionkey", "selectionid", "bookmakermarketselectionid",
    "outcomeid", "marketid", "marketkey", "selection",
})


def clean_key(key: object) -> str:
    return "".join(c for c in str(key).lower() if c.isalnum())


def absolute_timestamp(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return value.tzinfo is not None and value.utcoffset() is not None


def audit_structure(data: object, *, max_nodes: int = NODES_MAX) -> dict:
    if max_nodes < 1:
        raise ValueError("max_nodes must be positive")
    to_visit = [data]
    counts: Counter = Counter()
    keys: Counter = Counter()
    offer_shape_keys: Counter = Counter()
    # Metadata exists on dictionaries. Scalar values cannot contain quote
    # timestamps or bookmaker identities; skipping scalar traversal permits
    # a complete bounded scan of large cached snapshots.
    nodes = 0
    while to_visit and nodes < max_nodes:
        value = to_visit.pop()
        nodes += 1
        if isinstance(value, dict):
            counts["dictNodes"] += 1
            normalized = {clean_key(k): v for k, v in value.items()}
            keys.update(normalized.keys())
            has_price = bool(PRICE_KEYS.intersection(normalized))
            has_bookmaker = bool(BOOKMAKER_KEYS.intersection(normalized))
            has_selection = bool(SELECTION_KEYS.intersection(normalized))
            observed_keys = TIMESTAMP_KEYS.intersection(normalized)
            absolute = any(absolute_timestamp(normalized[k]) for k in observed_keys)
            if has_price:
                counts["priceLikeObjects"] += 1
                offer_shape_keys.update(normalized.keys())
                if has_bookmaker:
                    counts["priceLikeWithSameObjectBookmaker"] += 1
                if has_selection:
                    counts["priceLikeWithSameObjectSelection"] += 1
                if observed_keys:
                    counts["priceLikeWithSameObjectTimestampField"] += 1
                if absolute:
                    counts["priceLikeWithSameObjectAbsoluteTimestamp"] += 1
                if has_bookmaker and has_selection and absolute:
                    counts["potentiallySelfContainedOffers"] += 1
            if observed_keys:
                counts["objectsWithTimestampCandidateField"] += 1
                if absolute:
                    counts["objectsWithAbsoluteTimestampCandidate"] += 1
            to_visit.extend(v for v in normalized.values()
                            if isinstance(v, (dict, list)))
        elif isinstance(value, list):
            counts["listNodes"] += 1
            to_visit.extend(v for v in value if isinstance(v, (dict, list)))
    return {
        "visitedNodes": nodes,
        "scanTruncated": bool(to_visit),
        "rootType": type(data).__name__,
        "rootKeys": sorted(str(k) for k in data)[:50] if isinstance(data, dict) else [],
        "counts": dict(sorted(counts.items())),
        "observedRelevantFieldNames": {
            k: keys[k] for k in sorted(keys)
            if k in TIMESTAMP_KEYS | PRICE_KEYS | BOOKMAKER_KEYS | SELECTION_KEYS
        },
        "priceLikeObjectFieldNames": {
            k: offer_shape_keys[k] for k in sorted(offer_shape_keys)
            if k in TIMESTAMP_KEYS | PRICE_KEYS | BOOKMAKER_KEYS | SELECTION_KEYS
        },
    }


def audit_file(root: Path) -> dict:
    source = (root.resolve() / SOURCE).resolve()
    if not source.is_relative_to(root.resolve()) or not source.is_file():
        raise ValueError("Required cached odds source missing or unsafe")
    size = source.stat().st_size
    if size > SOURCE_MAX_BYTES:
        raise ValueError("Cached odds source exceeds bounded research audit size")
    payload = source.read_bytes()
    data = json.loads(payload)
    result = audit_structure(data)
    return {
        "contract": "betting-v2-cached-odds-structural-audit-v1",
        "sourcePath": SOURCE,
        "sizeBytes": size,
        "sourceSha256": sha256(payload).hexdigest(),
        "providerCalls": 0,
        "researchOnly": True,
        "certifiedStrong": 0,
        "sourceTimestampIsNotPerQuoteObservation": True,
        "priceObservationTimestampVerified": False,
        "independentUnfilteredBookmakerUniverseVerified": False,
        "quoteIdentityAndAsOfProvenanceIndependentlyVerified": False,
        "warning": (
            "Field-name presence is NOT quote observation provenance. "
            "Requires source-specific verified bookmaker/market/selection mapping, "
            "per-offer observation time, generation proof and unbiased universe."
        ),
        "structure": result,
    }


def main() -> None:
    from publish_shadow import _safe_output, _atomic_write

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--report", type=Path,
                    default=Path("reports/betting_v2/pilot_cached_odds_provenance.json"))
    args = ap.parse_args()
    root = args.repository_root.resolve()
    result = audit_file(root)
    output = _safe_output(root, args.report)
    _atomic_write(output, result)
    print(json.dumps({
        "contract": result["contract"],
        "priceLikeObjects": result["structure"]["counts"].get("priceLikeObjects", 0),
        "scanTruncated": result["structure"]["scanTruncated"],
        "quoteProvenanceCertified": False,
        "providerCalls": 0,
        "report": output.relative_to(root).as_posix(),
    }))


if __name__ == "__main__":
    main()
