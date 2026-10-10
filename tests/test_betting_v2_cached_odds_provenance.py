"""Offline cached bookmaker source audit must never invent per-offer quote timestamps."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from audit_cached_odds_provenance import (
    absolute_timestamp, audit_file, audit_structure,
)


class CachedOddsProvenanceAuditTests(unittest.TestCase):
    def test_timezone_is_required(self):
        self.assertTrue(absolute_timestamp("2026-10-10T15:20:00Z"))
        self.assertTrue(absolute_timestamp("2026-10-10T15:20:00+00:00"))
        self.assertFalse(absolute_timestamp("2026-10-10T15:20:00"))
        self.assertFalse(absolute_timestamp(None))
        self.assertFalse(absolute_timestamp("2026-bad"))

    def test_generation_date_is_not_offer_timestamp(self):
        payload = {
            "generatedAt": "2026-10-10T15:20:00Z",
            "bookmakers": [{
                "bookmaker": "ExampleBook",
                "markets": [{
                    "selectionId": "A", "odd": 2.05,
                }],
            }],
        }
        result = audit_structure(payload)
        self.assertEqual(result["counts"]["priceLikeObjects"], 1)
        self.assertEqual(
            result["counts"].get("priceLikeWithSameObjectAbsoluteTimestamp", 0), 0
        )
        self.assertEqual(result["counts"].get("potentiallySelfContainedOffers", 0), 0)

    def test_even_source_timestamp_named_quote_observed_at_does_not_certify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "odds/odds_api_io/domestic_odds.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({
                "quotes": [{
                    "bookmaker": "TestBook",
                    "selectionKey": "home-win",
                    "odd": 2.0,
                    "quoteObservedAt": "2026-10-10T15:00:00Z"
                }]
            }), encoding="utf-8")
            report = audit_file(root)
            self.assertEqual(
                report["structure"]["counts"]["potentiallySelfContainedOffers"], 1
            )
            self.assertFalse(report["priceObservationTimestampVerified"])
            self.assertFalse(report["independentUnfilteredBookmakerUniverseVerified"])
            self.assertFalse(report["quoteIdentityAndAsOfProvenanceIndependentlyVerified"])
            self.assertEqual(report["certifiedStrong"], 0)
            self.assertEqual(report["providerCalls"], 0)

    def test_source_missing_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                audit_file(Path(tmp))

    def test_scan_ignores_scalar_nodes_and_covers_every_offer(self):
        payload = {"offers": [
            {"bookmaker": "B", "selection": f"s{i}", "odd": 2.0,
             "unimportant": "foo"}
            for i in range(20)
        ]}
        # root dict + offers list + 20 offer dictionaries = 22 nodes.
        result = audit_structure(payload, max_nodes=22)
        self.assertEqual(result["visitedNodes"], 22)
        self.assertFalse(result["scanTruncated"])
        self.assertEqual(result["counts"]["priceLikeObjects"], 20)

    def test_explicit_scan_limit_is_visible(self):
        result = audit_structure([{"odd": 2.05}, {"odd": 1.95}], max_nodes=1)
        self.assertTrue(result["scanTruncated"])
        self.assertEqual(result["visitedNodes"], 1)


if __name__ == "__main__":
    unittest.main()
