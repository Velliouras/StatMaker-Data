"""No injury, doubtful-player or confirmed-XI requirement for V2 production.

These offline regression fixtures never create a real prediction or issue a
provider call. The calibrated P(WIN)/EV gates remain tested separately.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.betting_v2.production_contract import digest
from scripts.betting_v2.production_context import adapt_context_receipts
from scripts.betting_v2.production_models import context_values
from scripts.betting_v2.produce_certified_app_ready import case_for


class OptionalInjuryContextTest(unittest.TestCase):
    def target(self):
        return {
            "fixture_id": "fixture-101", "home_team_id": "home-1",
            "away_team_id": "away-2", "kickoffEpochMs": 10_000_000,
            "leagueCode": "E0", "season": "2026",
        }

    def test_no_receipt_does_not_reject_real_fixture(self):
        source = {"schedule": [self.target()], "history": []}
        records, reasons = adapt_context_receipts(source, [], maximum_age_ms=100000)
        self.assertEqual(records, [])
        self.assertEqual(dict(reasons), {})

    def test_missing_injuries_are_not_misreported_as_fully_fit(self):
        target = self.target()
        names = ["homeUnavailablePlayers", "awayUnavailablePlayers"]
        self.assertEqual(context_values(None, target, 9_000_000, []), [])
        self.assertEqual(context_values(None, target, 9_000_000, names),
                         [0.0, 0.0, 0.0, 0.0])

    def test_empty_real_injury_response_does_not_need_doubtful_player(self):
        target = self.target()
        # An empty response is still a real successful receipt, not an
        # assumption or a synthetic provider event.
        receipt = {
            "contract": "statmaker-betting-v2-context-receipt-v1",
            "provider": "api-football", "endpoint": "/injuries",
            "parameters": {"ids": "fixture-101"},
            "clientReceivedAtMs": 9_100_000, "response": [],
            "responseSha256": digest([]),
        }
        records, reasons = adapt_context_receipts(
            {"schedule": [target], "history": []}, [receipt],
            maximum_age_ms=700_000)
        self.assertEqual(dict(reasons), {})
        self.assertEqual(len(records), 1)
        self.assertNotIn("adverse", records[0])
        self.assertEqual(
            context_values(records[0]["base"], target, 9_200_000,
                           ["homeUnavailablePlayers", "awayUnavailablePlayers"]),
            [0.0, 1.0, 0.0, 1.0],
        )

    def test_model_case_does_not_need_injury_archive(self):
        target = self.target()
        past = {
            "fixture_id": "past-1", "availableAtMs": 8_200_000,
            "season": "2026", "sourceSha256": "a"*64,
        }
        params = {"maximumFeatureAgeMs": 5_000_000}
        plan = {"family": "GOALS_FT", "mode": "ELO_GOALS_FALLBACK_NO_XG",
                "contextColumns": []}
        with patch("scripts.betting_v2.produce_certified_app_ready.features",
                   return_value=([1.0, 0.5], [past], [past])) as mocked:
            case = case_for([past], target, None, {}, params, plan, 9_000_000)
        self.assertEqual(case["features"], [1.0, 0.5])
        self.assertIsNone(case["context"])
        self.assertEqual(case["sourceHashes"], ["a"*64])
        self.assertNotIn("adverse", case)
        self.assertNotIn("adverseFeatures", case)
        self.assertEqual(mocked.call_count, 1)


if __name__ == "__main__":
    unittest.main()
