import copy
import json
import tempfile
import unittest
from pathlib import Path

from .daily import attach_daily, validate_daily


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state = self.directory / "daily_membership.json"

    def payload(self, time="2026-10-23T08:00:00Z", count=20):
        rows = [dict(selectionId=str(i), kickoff="2026-10-23T10:00:00Z",
                     quoteUpdatedAt="2026-10-23T07:00:00Z", outcome="PENDING") for i in range(count)]
        return dict(generatedAt=time, issued=rows, candidates=copy.deepcopy(rows))

    def publish(self, payload, number):
        result = attach_daily(payload, self.directory, self.state)
        (self.directory / f"candidates-{number}.json").write_text(json.dumps(result))
        return result

    def test_twenty_becomes_eighteen_when_two_prices_are_withdrawn(self):
        first = self.publish(self.payload(), 1)
        self.assertEqual(20, first["dailySelectionCount"])
        later = self.payload("2026-10-23T09:00:00Z")
        later["candidates"] = later["candidates"][:-2]
        original = copy.deepcopy(later["issued"])
        result = self.publish(later, 2)
        self.assertEqual([str(i) for i in range(18)], result["dailySelectionIds"])
        self.assertEqual(original, result["issued"])

    def test_kickoff_keeps_eighteen_and_does_not_revive_the_two_withdrawn(self):
        self.publish(self.payload(), 1)
        before = self.payload("2026-10-23T09:00:00Z")
        before["candidates"] = before["candidates"][:-2]
        self.publish(before, 2)
        after = self.payload("2026-10-23T10:01:00Z")
        after["candidates"] = []
        result = self.publish(after, 3)
        self.assertEqual(18, result["dailySelectionCount"])
        for row in after["issued"]:
            row.update(outcome="WON", settlementReturn=1.9)
        self.assertEqual(result["dailySelectionIds"], self.publish(after, 4)["dailySelectionIds"])

    def test_price_can_return_before_kickoff_and_counts_in_both(self):
        payload = self.payload(count=2)
        payload["candidates"] = []
        payload["priceUpdates"] = [dict(selectionId="1", observedAt="2026-10-23T07:50:00Z")]
        self.assertEqual(["1"], self.publish(payload, 1)["dailySelectionIds"])

    def test_bootstrap_uses_the_last_pre_match_snapshot_only(self):
        first = self.payload(count=3)
        (self.directory / "candidates-1.json").write_text(json.dumps(first))
        last = self.payload("2026-10-23T09:50:00Z", count=3)
        last["candidates"] = last["candidates"][:1]
        (self.directory / "candidates-2.json").write_text(json.dumps(last))
        after = self.payload("2026-10-23T10:10:00Z", count=3)
        after["candidates"] = []
        # A later result cannot put a withdrawn pre-match selection back in Daily.
        after["issued"][1]["outcome"] = "WON"
        self.assertEqual(["0"], self.publish(after, 3)["dailySelectionIds"])

    def test_quote_expiry_before_kickoff_is_not_frozen_as_eligible(self):
        before = self.payload(count=1)
        before["candidates"][0]["quoteUpdatedAt"] = "2026-10-22T09:00:00Z"
        self.publish(before, 1)
        after = self.payload("2026-10-23T10:01:00Z", count=1)
        after["candidates"] = []
        self.assertEqual([], self.publish(after, 2)["dailySelectionIds"])

    def test_missing_pre_match_evidence_does_not_admit_historical_issues(self):
        after = self.payload("2026-10-23T10:01:00Z", count=2)
        after["candidates"] = []
        self.assertEqual([], self.publish(after, 1)["dailySelectionIds"])

    def test_membership_rejects_duplicates_unknown_ids_and_divergence(self):
        for ids in (["0", "0"], ["unknown"], []):
            payload = self.payload(count=1)
            payload.update(dailySelectionIds=ids, dailySelectionCount=len(ids))
            with self.assertRaises(ValueError):
                validate_daily(payload)


if __name__ == "__main__":
    unittest.main()
