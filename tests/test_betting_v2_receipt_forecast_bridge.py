"""Offline strict receipt-model joins; reject ambiguous matches and absent forecasts."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from quote_receipt_capture import write_receipt
from receipt_forecast_bridge import normalized_market, fixture_resolution, provider_schedule_resolution, study
from walk_forward_goals import event_probs

OBSERVED = datetime(2026, 10, 10, 14, 30, tzinfo=timezone.utc)
KICKOFF = "2026-10-10T19:15:00Z"
PAYLOAD = [{
    "id": 123, "date": KICKOFF, "home": "Home FC", "away": "Away FC",
    "bookmakers": {"Bet365": [
        {"name": "ML", "odds": [
            {"home": "2.05", "draw": "3.30", "away": "3.90"}]},
        {"name": "Double Chance", "odds": [{"1X": "1.90", "12": "1.45"}]},
        {"name": "Totals", "odds": [{"hdp": 2.5, "over": "1.91", "under": "1.95"}]},
        {"name": "Spread", "odds": [{"hdp": -0.5, "home": "1.95"}]},
    ]}
}]
PARAMS = {"eventId": "123", "bookmakers": "Bet365"}


def lookup_fixture(*, ambiguous=False, valid=True):
    items = defaultdict(list)
    if valid:
        item = {
            "fixtureId": "fixture-123",
            "leagueCode": "BRA",
            "kickoffUTC": datetime.fromisoformat(KICKOFF.replace("Z", "+00:00")),
            "home": "home fc", "away": "away fc",
        }
        items[("BRA", "home fc", "away fc")].append(item)
        if ambiguous:
            items[("CUP", "home fc", "away fc")].append({
                **item, "fixtureId": "fixture-in-cup", "leagueCode": "CUP",
            })
    return SimpleNamespace(by_league_team=items)


class ReceiptForecastBridgeTests(unittest.TestCase):
    def test_fulltime_1x2_double_chance_and_goal_lines_map(self):
        expected = [
            ({"providerMarketName": "ML", "selection": "HOME", "odd": 2.1},
             "RESULT_1X2", "HOME"),
            ({"providerMarketName": "Double Chance", "selection": "1X", "odd": 1.9},
             "RESULT_DOUBLE_CHANCE", "HOME_OR_DRAW"),
            ({"providerMarketName": "Draw No Bet", "selection": "AWAY", "odd": 2.0},
             "RESULT_DNB", "AWAY"),
            ({"providerMarketName": "Totals", "selection": "OVER", "line": 2.5,
              "odd": 1.94}, "FULL_TIME_MATCH_TOTAL", "OVER"),
            ({"providerMarketName": "Team Total Home", "selection": "UNDER",
              "line": 3.5, "odd": 2.2}, "HOME_TEAM_TOTAL", "UNDER"),
        ]
        for row, market, direction in expected:
            with self.subTest(row=row):
                converted, status = normalized_market(row)
                self.assertEqual(status, "MAPPED_FULL_TIME_RESEARCH_MARKET")
                self.assertEqual((converted["market"], converted["direction"]),
                                 (market, direction))

    def test_asian_and_half_time_and_short_odds_rejected(self):
        invalid = [
            {"providerMarketName": "Spread", "selection": "HOME", "odd": 2},
            {"providerMarketName": "ML HT", "selection": "HOME", "odd": 2},
            {"providerMarketName": "Totals", "selection": "OVER", "line": 2.25,
             "odd": 2},
            {"providerMarketName": "Totals", "selection": "OVER", "line": 2.5,
             "odd": 1.8},
            {"providerMarketName": "ML", "selection": "HOME", "odd": 3.0},
        ]
        for row in invalid:
            with self.subTest(row=row):
                converted, _ = normalized_market(row)
                self.assertIsNone(converted)

    def test_unique_cross_league_matching_or_fail_closed(self):
        event = PAYLOAD[0]
        for ambiguous, valid, reason in [
            (False, True, "EXACT_CACHED_API_FIXTURE"),
            (True, True, "AMBIGUOUS_EXACT_API_FIXTURE"),
            (False, False, "NO_EXACT_CACHED_API_FIXTURE"),
        ]:
            with self.subTest(reason=reason):
                result, why = fixture_resolution(
                    event, lookup_fixture(ambiguous=ambiguous, valid=valid)
                )
                self.assertEqual(why, reason)
                self.assertEqual(result is not None, reason.startswith("EXACT"))

    def test_exact_odds_provider_schedule_is_not_api_football_identity(self):
        event = PAYLOAD[0]
        cached = {
            "123": [{
                "providerEventId": "123",
                "leagueCode": "BRA",
                "providerHomeTeam": "Home FC",
                "providerAwayTeam": "Away FC",
                "kickoffUTC": KICKOFF,
            }]
        }
        result, status = provider_schedule_resolution(event, cached)
        self.assertEqual(status, "EXACT_PROVIDER_SCHEDULE_ONLY_NOT_API_FIXTURE")
        self.assertEqual(result["leagueCode"], "BRA")
        duplicate = {**cached["123"][0], "leagueCode": "CUP"}
        _, status = provider_schedule_resolution(
            event, {"123": [cached["123"][0], duplicate]}
        )
        self.assertEqual(status, "AMBIGUOUS_PROVIDER_SCHEDULE_FIXTURE")
        mismatch = {**cached["123"][0], "providerAwayTeam": "Other FC"}
        _, status = provider_schedule_resolution(event, {"123": [mismatch]})
        self.assertEqual(status, "NO_EXACT_PROVIDER_SCHEDULE_FIXTURE")

    def test_provider_schedule_match_does_not_invent_model_predictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "odds/odds_api_io/domestic_odds.json"
            dest.parent.mkdir(parents=True)
            dest.write_text(json.dumps({
                "leagues": [{
                    "leagueCode": "BRA",
                    "matches": [{
                        "id": "123", "kickoff": KICKOFF,
                        "providerHomeTeam": "Home FC",
                        "providerAwayTeam": "Away FC",
                    }],
                }],
            }), encoding="utf-8")
            write_receipt(root, PAYLOAD, "/odds", PARAMS, OBSERVED)
            result = study(root, lookup=lookup_fixture(valid=False))
            self.assertEqual(result["distinctSameProviderScheduledFixtures"], 1)
            self.assertGreater(
                result["counts"]["verifiedSameProviderSchedulePriceObservations"], 0
            )
            self.assertFalse(result["sameProviderScheduleIsApiFootballIdentity"])
            self.assertEqual(result["distinctExactCachedFixtures"], 0)
            self.assertIsNone(result["certifiedEV"])

    def test_real_receipt_no_timestamps_invented_for_roi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, PAYLOAD, "/odds", PARAMS, OBSERVED)
            forecast = {
                "leagueCode": "BRA", "fixtureId": "fixture-123",
                "kickoffUTC": KICKOFF, "date": "2026-10-10",
                "probabilities": event_probs(1.4, 1.1),
                "homeGoals": 3, "awayGoals": 1,
            }
            p = root / "forecast.jsonl"
            p.write_text(json.dumps(forecast) + "\n", encoding="utf-8")
            result = study(root, p, lookup=lookup_fixture())
            self.assertEqual(result["counts"]["XG_PRIMARY_researchProbabilitiesMapped"], 4)
            self.assertEqual(result["distinctExactCachedFixtures"], 1)
            self.assertGreater(result["counts"]["mappedPriceObservations"], 0)
            self.assertIsNone(result["certifiedEV"])
            self.assertIsNone(result["certifiedROI"])
            self.assertEqual(result["certifiedStrong"], 0)
            self.assertEqual(result["providerCalls"], 0)

    def test_no_cached_id_or_no_forecast_never_claims_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_receipt(root, PAYLOAD, "/odds", PARAMS, OBSERVED)
            missing = study(root, lookup=lookup_fixture(valid=False))
            self.assertEqual(missing["distinctExactCachedFixtures"], 0)
            self.assertEqual(missing["rejected"]["NO_EXACT_CACHED_API_FIXTURE"], 4)
            linked = study(root, lookup=lookup_fixture())
            self.assertEqual(linked["distinctExactCachedFixtures"], 1)
            self.assertIsNone(linked["certifiedROI"])
            self.assertEqual(linked["certifiedStrong"], 0)


if __name__ == "__main__":
    unittest.main()
