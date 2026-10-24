import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import build
from scripts.shots_uat.daily import attach_daily, validate_daily
from scripts.shots_uat.feed import publish_read_model
from scripts.shots_uat.publish_production import publish
from scripts.shots_uat.repricing import timestamp, validate_price_updates


class MarketWatchFeedRefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "data/statmaker/uat/shots_v1"
        self.directory = self.base / "p55"
        self.directory.mkdir(parents=True)
        self.state = self.base / "daily_membership.json"

    def payload(self, kickoff="2026-10-23T17:29:00Z"):
        issue = dict(selectionId="repriced", apiFixtureId=1, bookmaker="Bet365",
            model="SHOTS_SOT_CATBOOST_V1", metric="shots", market="MATCH_SHOTS",
            selection="Over 20.5", team=None, line=20.5, kickoff=kickoff,
            publishedAt="2026-10-23T16:00:00Z", publicationCommit="a" * 40,
            quoteUpdatedAt="2026-10-23T15:00:00Z", latestQuoteObservedAt=None,
            odd=1.9, winProbability=.7, pushProbability=0., outcome="PENDING",
            match=dict(id="event-1"))
        regular = dict(issue, selectionId="regular", apiFixtureId=2,
            kickoff="2026-10-25T19:00:00Z", latestQuoteObservedAt="2026-10-23T17:27:00Z",
            match=dict(id="event-2"))
        update = {k: issue.get(k) for k in (
            "selectionId", "apiFixtureId", "bookmaker", "market", "selection",
            "team", "line", "kickoff")}
        update.update(providerEventId="event-1", originalOdd=1.9, odd=2.,
            observedAt="2026-10-23T17:27:00Z", indicativeEV=.4)
        return dict(schemaVersion=1, status="UAT_SHOTS_MODEL", apiCalls=0, prodWrites=0,
            generationId="initial", generatedAt="2026-10-23T17:28:00Z",
            issued=[issue, regular], marketIssued=[], candidates=[regular],
            candidateCount=1, marketCandidates=[], marketCandidateCount=0,
            priceUpdates=[update], priceUpdateCount=1)

    def stage(self, payload):
        validate_price_updates(payload)
        attach_daily(payload, self.directory, self.state)
        publish_read_model(self.directory, payload)

    def refresh(self, instant, allowed=True):
        with patch.object(build, "ROOT", self.root), patch.object(build, "allowed", return_value=allowed):
            build.refresh_current_feed(timestamp(instant))
        index = json.loads((self.directory / "latest.json").read_text())
        payload = json.loads((self.directory / index["artifact"]).read_text())
        validate_price_updates(payload)
        validate_daily(payload)
        publish(self.root)
        return payload

    def test_crossing_kickoff_drops_live_price_and_preserves_frozen_daily_and_history(self):
        original = self.payload()
        self.stage(original)
        result = self.refresh("2026-10-23T17:30:00Z")
        self.assertEqual([], result["priceUpdates"])
        self.assertEqual(0, result["priceUpdateCount"])
        self.assertEqual(["regular"], [r["selectionId"] for r in result["candidates"]])
        self.assertEqual({"repriced", "regular"}, set(result["dailySelectionIds"]))
        self.assertEqual(original["issued"], result["issued"])

    def test_future_price_stays_exact_and_does_not_revive_the_frozen_original_price(self):
        original = self.payload()
        original["issued"][0]["latestQuoteObservedAt"] = "2026-10-23T17:00:00Z"
        self.stage(original)
        result = self.refresh("2026-10-23T17:28:30Z")
        self.assertEqual(original["priceUpdates"], result["priceUpdates"])
        self.assertEqual(["regular"], [r["selectionId"] for r in result["candidates"]])
        self.assertEqual(original["issued"], result["issued"])

    def test_expired_observation_is_removed_from_future_singles_and_daily(self):
        self.stage(self.payload("2026-10-25T18:00:00Z"))
        result = self.refresh("2026-10-24T17:28:00Z")
        self.assertEqual([], result["priceUpdates"])
        self.assertEqual([], result["candidates"])
        self.assertEqual([], result["dailySelectionIds"])

    def test_pause_removes_live_overlay_and_daily_without_rewriting_issued_rows(self):
        original = self.payload()
        self.stage(original)
        result = self.refresh("2026-10-23T17:28:30Z", allowed=False)
        self.assertEqual([], result["priceUpdates"])
        self.assertEqual([], result["candidates"])
        self.assertEqual([], result["dailySelectionIds"])
        self.assertEqual(original["issued"], result["issued"])

    def test_pause_of_an_ordinary_candidate_also_updates_daily(self):
        original = self.payload()
        original["priceUpdates"] = []
        original["priceUpdateCount"] = 0
        self.stage(original)
        result = self.refresh("2026-10-23T17:28:30Z", allowed=False)
        self.assertEqual([], result["candidates"])
        self.assertEqual([], result["dailySelectionIds"])

    def test_invalid_source_price_is_rejected_even_if_kickoff_has_passed(self):
        original = self.payload()
        self.stage(original)
        index = json.loads((self.directory / "latest.json").read_text())
        corrupt = json.loads((self.directory / index["artifact"]).read_text())
        corrupt["priceUpdates"][0]["indicativeEV"] = .99
        publish_read_model(self.directory, corrupt)
        with self.assertRaises(ValueError):
            self.refresh("2026-10-23T17:30:00Z")


if __name__ == "__main__":
    unittest.main()

