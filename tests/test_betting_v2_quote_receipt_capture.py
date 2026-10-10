"""No-network tests for append-only provider HTTP receipts and fail-closed claims."""
from __future__ import annotations

import datetime as dt
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from betting_v2.quote_receipt_capture import (
    canonical, install, receipt_document, verify_receipt, write_receipt,
)

NOW = dt.datetime(2026, 10, 10, 14, 32, 4, tzinfo=dt.timezone.utc)
SOURCE = [{
    "id": 123,
    "bookmakers": [{
        "name": "Bet365",
        "markets": [{"name": "1X2", "odds": [
            {"name": "Home", "odds": 2.15},
            {"name": "Draw", "odds": 3.20},
            {"name": "Away", "odds": 3.40},
        ]}],
    }],
}]
PARAMS = {"apiKey": "DO_NOT_WRITE_THIS_SECRET", "eventIds": "123", "bookmakers": "Bet365"}


class CaptureReceiptTests(unittest.TestCase):
    def test_evidence_never_claims_provider_quote_timestamp_or_certificate(self):
        receipt = receipt_document(SOURCE, "/odds/multi", PARAMS, NOW)
        self.assertFalse(receipt["priceObservationTimestampVerified"])
        self.assertFalse(receipt["bookmakerMarketSelectionIdVerified"])
        self.assertFalse(receipt["independentUnfilteredBookmakerUniverseVerified"])
        self.assertFalse(receipt["certifiedStrong"])
        self.assertEqual(receipt["extraProviderRequests"], 0)
        self.assertEqual(receipt["clientReceivedAtUTC"], "2026-10-10T14:32:04Z")
        self.assertEqual(receipt["decodedResponseSha256"], sha256(canonical(SOURCE)).hexdigest())
        self.assertNotIn("apiKey", json.dumps(receipt))
        self.assertNotIn("DO_NOT_WRITE_THIS_SECRET", json.dumps(receipt))

    def test_store_and_read_complete_decoded_response_immutably(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = write_receipt(root, SOURCE, "/odds/multi", PARAMS, NOW)
            two = write_receipt(root, SOURCE, "/odds/multi", PARAMS, NOW)
            self.assertNotEqual(one, two)
            with gzip.open(one, "rt", encoding="utf-8") as f:
                receipt = json.load(f)
            self.assertNotIn("decodedResponse", receipt)
            self.assertFalse(receipt["fullDecodedResponseStored"])
            self.assertFalse(receipt["rawProviderBodyPublished"])
            self.assertTrue(receipt["marketSnapshotComplete"])
            self.assertEqual(receipt["decodedResponseBytes"], len(canonical(SOURCE)))
            self.assertEqual(receipt["boundedMarketSnapshot"][0]["event"]["id"], 123)
            self.assertEqual(receipt["boundedMarketSnapshot"][0]["bookmakers"][0]["markets"][0]["outcomes"][0]["odds"], 2.15)
            self.assertEqual(len(list((root / "reports/betting_v2/quote_receipts").glob("*.gz"))), 2)

    def test_oversized_response_is_metadata_only_not_falsely_complete(self):
        receipt = receipt_document(SOURCE, "/odds/multi", PARAMS, NOW, max_decoded_bytes=1)
        self.assertFalse(receipt["fullDecodedResponseStored"])
        self.assertFalse(receipt["marketSnapshotComplete"])
        self.assertEqual(receipt["boundedMarketSnapshot"], [])
        self.assertGreater(receipt["decodedResponseBytes"], 1)

    def test_client_time_must_be_timezone_aware(self):
        with self.assertRaises(ValueError):
            receipt_document(SOURCE, "/odds", PARAMS, dt.datetime(2026, 10, 10))
        with self.assertRaises(ValueError):
            receipt_document(SOURCE, "/events", PARAMS, NOW)

    def test_verified_local_hash_does_not_imply_certified_odds(self):
        receipt = receipt_document(SOURCE, "/odds/multi", PARAMS, NOW)
        verified = verify_receipt(receipt)
        self.assertTrue(verified["verifiedLocalReceiptIntegrity"])
        self.assertEqual(verified["sourceGenerationId"],
                         receipt["decodedResponseSha256"])
        self.assertFalse(verified["providerOfferTimestampIndependentlyVerified"])
        self.assertFalse(verified["readyForCertifyingStrong"])

    def test_altered_or_missing_response_is_never_verified(self):
        receipt = receipt_document(SOURCE, "/odds/multi", PARAMS, NOW)
        modified = dict(receipt)
        modified["boundedMarketSnapshot"] = []
        with self.assertRaises(ValueError):
            verify_receipt(modified)
        modified = dict(receipt)
        modified["clientReceivedAtUTC"] = "2026-10-10T14:32:04"
        with self.assertRaises(ValueError):
            verify_receipt(modified)
        modified = receipt_document(
            SOURCE, "/odds/multi", PARAMS, NOW, max_decoded_bytes=1
        )
        with self.assertRaises(ValueError):
            verify_receipt(modified)
        modified = dict(receipt)
        modified["priceObservationTimestampVerified"] = True
        with self.assertRaises(ValueError):
            verify_receipt(modified)

    def test_sanitized_public_receipt_omits_unrelated_raw_provider_fields(self):
        payload = [{
            "id": 11, "randomRawSecret": "DO_NOT_PUBLISH_RAW",
            "bookmakers": [{"name": "B", "internalProviderSecret": "DO_NOT_PUBLISH_RAW",
                            "markets": [{"name": "1X2", "adminToken": "DO_NOT_PUBLISH_RAW",
                                         "odds": [{"name": "Home", "odds": 1.95,
                                                   "apiKey": "HIDDEN"}]}]}]
        }]
        receipt = receipt_document(payload, "/odds", {"eventId": "11"}, NOW)
        published = json.dumps(receipt)
        self.assertNotIn("DO_NOT_PUBLISH_RAW", published)
        self.assertNotIn("HIDDEN", published)
        self.assertNotIn("adminToken", published)
        self.assertTrue(receipt["marketSnapshotComplete"])
        self.assertNotIn("decodedResponse", receipt)

    def test_unsupported_bookmaker_shapes_are_not_certified(self):
        payload = [{"id": 11, "bookmakers": [{"name": "B", "markets": ["unexpected"]}]}]
        receipt = receipt_document(payload, "/odds", {"eventId": "11"}, NOW)
        self.assertFalse(receipt["marketSnapshotComplete"])
        with self.assertRaises(ValueError):
            verify_receipt(receipt)

    def test_hook_returns_same_provider_response_without_repoll(self):
        calls = []
        def api_get(path, params, debug, *, allow_error=True):
            calls.append(path)
            return SOURCE
        module = SimpleNamespace(api_get=api_get)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install(module, root, now_fn=lambda: NOW)
            install(module, root, now_fn=lambda: NOW)
            debug = {}
            result = module.api_get("/odds/multi", PARAMS, debug)
            self.assertIs(result, SOURCE)
            self.assertEqual(calls, ["/odds/multi"])
            self.assertEqual(len(debug["bettingV2ReceiptPaths"]), 1)
            self.assertEqual(debug.get("bettingV2ReceiptWarnings", []), [])
            result = module.api_get("/events", PARAMS, debug)
            self.assertIs(result, SOURCE)
            self.assertEqual(calls, ["/odds/multi", "/events"])
            self.assertEqual(len(debug["bettingV2ReceiptPaths"]), 1)

    def test_no_receipt_on_error_or_empty_payload(self):
        for payload in (None, "bad"):
            calls = []
            def api_get(path, params, debug, *, allow_error=True):
                calls.append(1)
                return payload
            module = SimpleNamespace(api_get=api_get)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                install(module, root, now_fn=lambda: NOW)
                result = module.api_get("/odds", {"eventId": "1"}, {})
                self.assertIs(result, payload)
                self.assertEqual(len(calls), 1)
                self.assertFalse((root / "reports/betting_v2/quote_receipts").exists())


if __name__ == "__main__":
    unittest.main()
