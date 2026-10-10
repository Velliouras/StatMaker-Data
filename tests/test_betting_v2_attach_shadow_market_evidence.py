"""Live cached bookmaker odds can enter UAT as UNVERIFIED research evidence only."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from attach_shadow_market_evidence import attach, evidence, pair_matches, probability


FORECAST = {
    "providerEventId": "odds-123",
    "leagueCode": "BRA",
    "homeTeam": "Home FC",
    "awayTeam": "Away FC",
    "kickoffUTC": "2026-10-11T20:30:00Z",
    "probabilities": {
        "1X2_HOME": 0.5, "1X2_DRAW": 0.25, "1X2_AWAY": 0.25,
        "MATCH_OVER_2_5": 0.55, "HOME_OVER_1_5": 0.37,
    },
}
MATCH = {
    "id": "odds-123",
    "kickoff": "2026-10-11T20:30:00+00:00",
    "providerHomeTeam": "Home FC",
    "providerAwayTeam": "Away FC",
    "teamMappingStatus": "matched",
    "scheduleVerified": True,
    "markets": [
        {"market": "1X2", "selection": "Home", "odds": 2.2,
         "bookmaker": "Bet365", "exactBookmakerOdds": True},
        {"market": "MATCH_GOALS", "selection": "Over", "line": 2.5,
         "odds": 2.0, "bookmaker": "Bet365", "exactBookmakerOdds": True},
        {"market": "MATCH_GOALS", "selection": "Under", "line": 2.5,
         "odds": 1.65, "bookmaker": "Bet365", "exactBookmakerOdds": True},
        {"market": "TEAM_TOTAL_GOALS", "team": "Home FC",
         "selection": "Over", "line": 1.5, "odds": 2.7,
         "bookmaker": "Bet365", "exactBookmakerOdds": True},
        {"market": "MATCH_CORNERS", "selection": "Over", "line": 8.5,
         "odds": 1.99, "bookmaker": "Bet365", "exactBookmakerOdds": True},
        {"market": "ASIAN_HANDICAP", "selection": "Home", "line": -0.25,
         "odds": 1.99, "bookmaker": "Bet365", "exactBookmakerOdds": True},
    ],
}


class PricedShadowTests(unittest.TestCase):
    def test_market_mapping_never_includes_asian_or_corners(self):
        found = evidence(FORECAST, MATCH)
        names = {x["market"] for x in found}
        self.assertIn("MATCH_GOALS", names)
        self.assertIn("1X2", names)
        self.assertIn("TEAM_TOTAL_GOALS", names)
        self.assertNotIn("MATCH_CORNERS", names)
        self.assertNotIn("ASIAN_HANDICAP", names)
        for item in found:
            self.assertFalse(item["certifiedStrong"])
            self.assertFalse(item["bookmakerPriceUpdatedAtVerified"])
            self.assertFalse(item["EV_Certified"])
        first = next(x for x in found if x["market"] == "1X2")
        self.assertAlmostEqual(first["indicativeEVNotCertified"], 0.1)

    def test_exact_provider_event_and_clocks_required(self):
        self.assertTrue(pair_matches(FORECAST, MATCH, "BRA"))
        self.assertFalse(pair_matches(FORECAST, {**MATCH, "id": "not123"}, "BRA"))
        self.assertFalse(pair_matches(FORECAST, {**MATCH, "providerHomeTeam": "Other"}, "BRA"))
        self.assertFalse(pair_matches(FORECAST, {**MATCH, "kickoff": "2026-10-11T21:00:00Z"}, "BRA"))
        self.assertFalse(pair_matches(FORECAST, {**MATCH, "scheduleVerified": False}, "BRA"))

    def test_no_prices_without_verified_bookmaker_field(self):
        altered = {**MATCH, "markets": [
            {**MATCH["markets"][0], "exactBookmakerOdds": False}
        ]}
        self.assertEqual(evidence(FORECAST, altered), [])

    def test_uncertified_value_unavailable_as_approved_bet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            preview = root / "reports/betting_v2/pilot_forward_shadow_preview.json"
            preview.parent.mkdir(parents=True)
            preview.write_text(json.dumps({
                "contract": "statmaker-v2-uat-forecast-preview-v1",
                "certificationStatus": "BLOCKED",
                "researchOnly": True, "certifiedStrong": 0,
                "matches": [dict(FORECAST)],
            }))
            feed = root / "odds/odds_api_io/domestic_odds.json"
            feed.parent.mkdir(parents=True)
            feed.write_text(json.dumps({
                "leagues": [{"leagueCode": "BRA", "matches": [MATCH]}]
            }))
            result = attach(root)
            self.assertEqual(result["fixturesWithSupportedMarketEvidence"], 1)
            self.assertEqual(result["sameProviderLiveOddsFixtureLinks"], 1)
            self.assertIs(result["matches"][0]["marketEvidence"][0]["EV_Certified"], False)
            self.assertEqual(result["certifiedStrong"], 0)
            self.assertEqual(result["apiCalls"], 0)

    def test_duplicate_schedule_matches_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            preview = root / "reports/betting_v2/pilot_forward_shadow_preview.json"
            preview.parent.mkdir(parents=True)
            preview.write_text(json.dumps({
                "contract": "statmaker-v2-uat-forecast-preview-v1",
                "certificationStatus": "BLOCKED",
                "researchOnly": True, "certifiedStrong": 0,
                "matches": [dict(FORECAST)],
            }))
            feed = root / "odds/odds_api_io/domestic_odds.json"
            feed.parent.mkdir(parents=True)
            feed.write_text(json.dumps({
                "leagues": [{"leagueCode": "BRA", "matches": [MATCH, MATCH]}]
            }))
            result = attach(root)
            self.assertEqual(result["fixturesWithSupportedMarketEvidence"], 0)
            self.assertEqual(result["matches"][0]["marketEvidence"], [])


if __name__ == "__main__":
    unittest.main()
