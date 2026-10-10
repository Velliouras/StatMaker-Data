#!/usr/bin/env python3
"""Offline diagnostics of declined bookmaker market receipts; never fetches odds."""
from __future__ import annotations

from collections import Counter
import gzip
import json
from pathlib import Path

from quote_receipt_capture import STATUS_PATH, verify_receipt


def diagnose(root: Path) -> dict:
    files = sorted((root / STATUS_PATH).glob("*.json.gz"))
    reasons = Counter()
    characteristics = Counter()
    for f in files:
        try:
            if f.is_symlink() or f.stat().st_size > 350_000:
                reasons["oversized_or_unsafe_file"] += 1
                continue
            with gzip.open(f, "rb") as stream:
                raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                reasons["oversized_decompressed_file"] += 1
                continue
            doc = json.loads(raw)
            if isinstance(doc, dict):
                characteristics[
                    "market_snapshot_complete_" + str(
                        doc.get("marketSnapshotComplete")
                    ).lower()
                ] += 1
                characteristics[
                    "snapshot_bytes_zero_" + str(
                        not bool(doc.get("marketSnapshotBytes"))
                    ).lower()
                ] += 1
            verify_receipt(doc)
            reasons["accepted"] += 1
        except ValueError as exc:
            # Record exact known verifier diagnosis, never user data or payload.
            code = str(exc)
            if code not in {
                "Unknown quote receipt contract",
                "A quote receipt must not claim betting certification",
                "Unexpected endpoint",
                "Missing event identity or secret in request",
                "Client receipt timestamp missing",
                "Malformed client receipt timestamp",
                "Client receipt must be timezone aware",
                "Raw provider response must not be published in public Git",
                "Incomplete offer snapshot cannot prove price evidence",
                "Stored offer snapshot byte count mismatch",
                "Stored offer snapshot integrity mismatch",
                "Unverified individual quote cannot claim provenance",
            }:
                code = "other_value_error"
            reasons[code] += 1
        except (OSError, EOFError, TypeError, KeyError, json.JSONDecodeError):
            reasons["parse_or_filesystem_error"] += 1
    return {
        "contract": "betting-v2-offline-receipt-diagnostics-v1",
        "researchOnly": True,
        "providerCalls": 0,
        "totalFiles": len(files),
        "reasons": dict(sorted(reasons.items())),
        "characteristics": dict(sorted(characteristics.items())),
    }


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    result = diagnose(root)
    output = root / "reports/betting_v2/pilot_receipt_diagnostics.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
