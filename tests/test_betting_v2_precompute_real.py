"""Regression tests for real-data-only precomputed forward UAT suggestions."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from precompute_real_betting_v2 import compute

NOW = datetime(2026, 10, 11, 6, 0, tzinfo=timezone.utc)

def fake(*, verified=True, ev=0.12, kickoff=None, event="fixture123"):
    at = (NOW - timedelta(hours=1)).isoformat()
    start = (kickoff or NOW + timedelta(hours=7)).isoformat()
    return {
        "leagueCode": "BRA", "providerEventId": event,
        "independentApiFootballFixtureVerified": verified,
        "independentApiFootballFixtureId": "api123" if verified else None,
        "homeTeam": "A", "awayTeam": "B", "model": "ELO_GOALS_FALLBACK_NO_XG",
        "kickoffUTC": start, "forecastComputedAtUTC": at,
        "certifiedStrong": False,
        "marketEvidence": [{
            "market": "MATCH_GOALS", "selection": "Over", "line": 2.5,
            "bookmaker": "Bet365", "odds": 2.0,
            "modelProbability": (1.0 + ev)/2,
            "indicativeEVNotCertified": ev,
            "certifiedStrong": False,
            "EV_Certified": False,
            "bookmakerPriceUpdatedAtVerified": False,
        }],
    }

def source(matches):
    return {
        "contract": "statmaker-v2-uat-forecast-preview-v1",
        "certificationStatus": "BLOCKED", "researchOnly": True,
        "certifiedStrong": 0, "bookmakerOddsCertified": False,
        "EV_Certified": False, "matches": matches,
    }

class RealPrecomputeTests(unittest.TestCase):
    def test_real_observed_quote_becomes_provisional_solid(self):
        doc = compute(source([fake()]), now=NOW)
        self.assertEqual(doc["suggestionCount"], 1)
        self.assertEqual(doc["suggestions"][0]["grade"], "SOLID")
        self.assertEqual(doc["suggestions"][0]["market"], "MATCH_GOALS")
        self.assertFalse(doc["suggestions"][0]["certifiedStrong"])
        self.assertFalse(doc["EV_Certified"])
        self.assertEqual(doc["apiCalls"], 0)
        self.assertEqual(doc["matches"][0]["precomputedSelection"]["odds"], 2.0)

    def test_no_imaginary_match_or_odds_without_independent_id(self):
        x = compute(source([fake(verified=False), fake(event="later", ev=0.01)]), now=NOW)
        self.assertEqual(x["suggestionCount"], 0)
        self.assertEqual(x["rejected"]["MISSING_INDEPENDENT_FIXTURE_ID"], 1)
        self.assertEqual(x["rejected"]["NO_ELIGIBLE_REAL_MARKET"], 1)

    def test_exclude_started_match_and_duplicated_event(self):
        x = compute(source([
            fake(kickoff=NOW - timedelta(minutes=2)),
            fake(event="valid"), fake(event="valid"),
        ]), now=NOW)
        self.assertEqual(x["suggestionCount"], 1)
        self.assertEqual(x["rejected"]["NOT_UPCOMING_PREMATCH_FORECAST"], 1)
        self.assertEqual(x["rejected"]["DUPLICATE_FIXTURE"], 1)

    def test_block_asian_or_untrusted_bookmaker_quote(self):
        row = fake()
        row["marketEvidence"][0]["market"] = "ASIAN_HANDICAP"
        x = compute(source([row]), now=NOW)
        self.assertEqual(x["suggestionCount"], 0)
        row = fake()
        row["marketEvidence"][0]["bookmakerPriceUpdatedAtVerified"] = True
        self.assertEqual(compute(source([row]), now=NOW)["suggestionCount"], 0)

    def test_reject_fake_certified_contract_and_invalid_ev(self):
        src = source([fake()])
        src["certificationStatus"] = "READY"
        with self.assertRaises(ValueError):
            compute(src, now=NOW)
        row = fake()
        row["marketEvidence"][0]["indicativeEVNotCertified"] = 0.8
        x = compute(source([row]), now=NOW)
        self.assertEqual(x["suggestionCount"], 0)
        self.assertEqual(x["rejected"]["PRICE_PROBABILITY_EV_INCONSISTENT"], 1)

if __name__ == "__main__":
    unittest.main()
