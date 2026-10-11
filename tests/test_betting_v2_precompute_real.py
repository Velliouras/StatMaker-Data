"""Regression: master spec Strong-only real-source precompute never invents certificates."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from precompute_real_betting_v2 import compute, MIN_ODD, MAX_ODD

NOW = datetime(2026, 10, 11, 6, tzinfo=timezone.utc)


def fixture(*, odd=2.20, prob=0.65, independently_verified=True,
            certificate=None, event="123", kickoff=None):
    value = prob * odd - 1.0
    result = {
        "leagueCode": "BRA", "providerEventId": event,
        "independentApiFootballFixtureId": "api-123" if independently_verified else None,
        "independentApiFootballFixtureVerified": independently_verified,
        "homeTeam": "Team A", "awayTeam": "Team B",
        "model": "ELO_GOALS_FALLBACK_NO_XG",
        "forecastComputedAtUTC": (NOW - timedelta(hours=2)).isoformat(),
        "kickoffUTC": (kickoff or NOW + timedelta(hours=8)).isoformat(),
        "certifiedStrong": False,
        "marketEvidence": [{
            "market": "MATCH_GOALS", "selection": "OVER", "line": 2.5,
            "bookmaker": "real provider", "odds": odd,
            "modelProbability": prob,
            "indicativeEVNotCertified": value,
            "EV_Certified": False,
            "bookmakerPriceUpdatedAtVerified": False,
            "certifiedStrong": False,
        }],
    }
    if certificate is not None:
        result["independentConservativeForecastCertificate"] = certificate
    return result


def source(items):
    return {
        "contract": "statmaker-v2-uat-forecast-preview-v1",
        "certificationStatus": "BLOCKED",
        "researchOnly": True, "certifiedStrong": 0,
        "bookmakerOddsCertified": False, "EV_Certified": False,
        "matches": items,
    }


class MasterSpecStrongOnlyPrecomputeTests(unittest.TestCase):
    def test_strict_odds_limits(self):
        self.assertEqual((MIN_ODD, MAX_ODD), (1.80, 3.00))
        rows = [fixture(odd=1.80), fixture(odd=3.00, event="b"),
                fixture(odd=2.20, event="c")]
        report = compute(source(rows), now=NOW)
        self.assertEqual(report["suggestionCount"], 0)
        self.assertEqual(report["rejected"]["NO_STRICT_ODDS_RAW_PROBABILITY_SCREEN"], 2)
        self.assertEqual(report["rejected"]["MISSING_INDEPENDENT_CONSERVATIVE_CALIBRATION"], 1)

    def test_raw_high_probability_is_never_strong(self):
        report = compute(source([fixture(prob=0.86)]), now=NOW)
        self.assertEqual(report["suggestions"], [])
        self.assertIsNone(report["matches"][0]["precomputedSelection"])
        self.assertEqual(report["rejected"]["MISSING_INDEPENDENT_CONSERVATIVE_CALIBRATION"], 1)
        self.assertEqual(report["certifiedStrong"], 0)
        self.assertNotIn("SOLID", str(report["suggestions"]))
        self.assertEqual(report["thresholds"]["allowedTiers"],
                         ["HIGH", "CONFIRMED", "DEVELOPING"])

    def test_unvalidated_self_claimed_certificate_does_not_pass(self):
        report = compute(source([fixture(certificate={
            "outOfTimeValidated": True,
            "conservativeWinProbability": 0.99,
            "adverseScenarioConservativeWinProbability": 0.99,
            "conservativeExpectedValue": 1.0,
        })]), now=NOW)
        self.assertEqual(report["suggestionCount"], 0)
        self.assertEqual(report["rejected"]["CERTIFICATE_IMPORTER_NOT_YET_VERIFIED"], 1)

    def test_no_fixture_id_no_recommendation(self):
        report = compute(source([fixture(independently_verified=False)]), now=NOW)
        self.assertEqual(report["suggestionCount"], 0)
        self.assertEqual(report["rejected"]["MISSING_INDEPENDENT_FIXTURE_ID"], 1)

    def test_reject_started_and_duplicated_games(self):
        report = compute(source([
            fixture(kickoff=NOW - timedelta(minutes=1)),
            fixture(event="b"), fixture(event="b"),
        ]), now=NOW)
        self.assertEqual(report["suggestionCount"], 0)
        self.assertEqual(report["rejected"]["NOT_UPCOMING_PREMATCH_FORECAST"], 1)
        self.assertEqual(report["rejected"]["DUPLICATE_FIXTURE"], 1)

    def test_contract_must_be_explicitly_research_only(self):
        doc = source([fixture()])
        doc["certificationStatus"] = "CERTIFIED"
        with self.assertRaises(ValueError):
            compute(doc, now=NOW)


if __name__ == "__main__":
    unittest.main()
