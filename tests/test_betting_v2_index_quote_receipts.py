"""Offline regressions for market/price evidence from actual observed provider replies."""
from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from quote_receipt_capture import write_receipt, install, verify_receipt
from index_quote_receipts import extracted_offers, inspect

NOW = dt.datetime(2026, 10, 10, 14, 32, tzinfo=dt.timezone.utc)
EVENTS = [{"id": 123, "date": "2026-10-10T19:15:00Z",
           "home": "Home FC", "away": "Away FC"}]
ODDS = [{"id": 123, "bookmakers": [{"name": "Bet365", "markets": [
    {"name": "1X2", "odds": [{"name": "Home", "odds": 2.10},
                            {"name": "Draw", "odds": 3.30},
                            {"name": "Away", "odds": 3.50}]},
    {"name": "Match Goals", "odds": [{"hdp": 2.5, "over": 1.90, "under": 1.95}]},
]}]}]
REQUEST = {"apiKey": "NEVER_STORE_ME", "eventIds": "123", "bookmakers": "Bet365"}
CONTEXT = {"123": EVENTS[0]}


class QuoteReceiptIndexTests(unittest.TestCase):
    def test_quote_rows_join_only_to_matching_provider_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = write_receipt(root, ODDS, "/odds/multi", REQUEST, NOW,
                                   event_context=CONTEXT)
            with gzip.open(target, "rt", encoding="utf-8") as f:
                doc = json.load(f)
            status = verify_receipt(doc)
            self.assertTrue(status["marketSnapshotComplete"])
            self.assertFalse(status["readyForCertifyingStrong"])
            rows, rejects = extracted_offers(doc)
            self.assertEqual(len(rows), 5)
            self.assertFalse(rejects)
            self.assertEqual({x["providerEventId"] for x in rows}, {"123"})
            self.assertEqual({x["providerMarketName"] for x in rows},
                             {"1X2", "Match Goals"})
            self.assertTrue(all(not x["priceObservationTimestampVerified"] for x in rows))
            result = inspect(root)
            self.assertEqual(result["verifiedSnapshotReceipts"], 1)
            self.assertEqual(result["preKickoffPriceRows"], 5)
            self.assertEqual(result["uniqueProviderEvents"], 1)
            self.assertEqual(result["byMarket"]["1X2"], 3)
            self.assertEqual(result["byMarket"]["Match Goals"], 2)

    def test_odds_without_matching_event_context_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, ODDS, "/odds/multi", REQUEST, NOW,
                          event_context={"999": EVENTS[0]})
            report = inspect(root)
            self.assertEqual(report["preKickoffPriceRows"], 0)
            self.assertEqual(report["rejected"]["missing_event_or_absolute_kickoff"], 1)

    def test_client_receipt_after_kickoff_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            later = dt.datetime(2026, 10, 10, 20, tzinfo=dt.timezone.utc)
            write_receipt(root, ODDS, "/odds/multi", REQUEST, later,
                          event_context=CONTEXT)
            report = inspect(root)
            self.assertEqual(report["preKickoffPriceRows"], 0)
            self.assertEqual(report["rejected"]["received_at_or_after_kickoff"], 1)

    def test_actual_events_hook_uses_no_additional_provider_requests(self):
        calls = []
        def fetch(path, params, debug, *, allow_error=True):
            calls.append(path)
            return EVENTS if path == "/events" else ODDS
        module = SimpleNamespace(api_get=fetch)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install(module, root, now_fn=lambda: NOW)
            module.api_get("/events", {"apiKey": "NO"}, {})
            module.api_get("/odds/multi", REQUEST, {})
            self.assertEqual(calls, ["/events", "/odds/multi"])
            self.assertEqual(inspect(root)["preKickoffPriceRows"], 5)

    def test_corrupt_receipt_rejected_not_brought_into_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = write_receipt(root, ODDS, "/odds/multi", REQUEST, NOW,
                                   event_context=CONTEXT)
            with gzip.open(target, "wt", encoding="utf-8") as f:
                json.dump({"certifiedStrong": True}, f)
            report = inspect(root)
            self.assertEqual(report["preKickoffPriceRows"], 0)
            self.assertEqual(report["rejected"]["invalid_receipt"], 1)


if __name__ == "__main__":
    unittest.main()
