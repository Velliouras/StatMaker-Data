"""Pure offline regression: no provider requests or GitHub Actions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))

from calibration_gate import evaluate as evaluate_calibration
from export_historical_prices import verified_archive_timestamp


class BettingV2ArchiveIntegrityTests(unittest.TestCase):
    def test_verified_recent_archive_timestamp(self):
        cutoff = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
        recent = {"generatedAt": "2026-10-10T07:00:00Z"}
        self.assertIsNotNone(verified_archive_timestamp(recent, cutoff))

    def test_stale_future_missing_and_naive_bundle_timestamps_rejected(self):
        cutoff = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
        for raw in ("2026-10-08T06:00:00Z", "2026-10-10T09:00:00Z",
                    "2026-10-10T07:00:00", "", None):
            with self.subTest(timestamp=raw):
                self.assertIsNone(verified_archive_timestamp(
                    {"generatedAt": raw}, cutoff))


class BettingV2CalibrationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.forecast = {
            "league": "E0|England", "leagueCode": "E0", "fixtureId": "9876",
            "date": "2026-10-10", "kickoffUTC": "2026-10-10T22:30:00Z",
            "homeGoals": 2, "awayGoals": 1,
            "probabilities": {
                "1X2_HOME": 0.61, "1X2_DRAW": 0.23,
                "1X2_AWAY": 0.16
            },
            "observed": {"1X2_HOME": 1, "1X2_DRAW": 0, "1X2_AWAY": 0}
        }
        self.quote = {
            "date": "2026-10-11", "leagueCode": "E0", "fixtureId": "9876",
            "kickoffUTC": "2026-10-10T22:30:00Z",
            "quoteCutoff": "2026-10-11T00:00:00+03:00",
            "identityResolution": "EXACT_CACHED_FIXTURE",
            "selectionKey": "win-home", "market": "RESULT_1X2",
            "direction": "HOME", "odd": 2.0,
            "priceUniverse": "INDEPENDENT_VERIFIED_BOOKMAKER_OFFERS",
            "priceObservationTimestampVerified": True,
            "independentUnfilteredBookmakerUniverseVerified": True,
            "quoteObservedAt": "2026-10-10T20:00:00Z",
            "bookmaker": "SyntheticBookmaker",
            "bookmakerMarketSelectionId": "synthetic-home-9876",
            "sourceGenerationId": "synthetic-generation-A"
        }
        self.calib = {**self.forecast, "date": "2026-09-01",
                      "fixtureId": "1234", "kickoffUTC": "2026-09-01T18:00:00Z"}

    def test_duplicate_quote_is_not_double_counted(self):
        report = evaluate_calibration(
            [self.calib], [self.forecast], [self.quote, dict(self.quote)])
        self.assertEqual(report["marketCounts"]["1X2_HOME"]["evaluated"], 1)
        self.assertEqual(report["rejected"]["duplicate_selection_quote"], 1)
        self.assertFalse(report["independentUnfilteredBookmakerUniverseVerified"])
        self.assertIn("INDEPENDENT_VERIFIED_BOOKMAKER_OFFERS",
                      report["observedPriceUniverses"])

    def test_legacy_prepared_quote_never_becomes_calibrated_evidence(self):
        legacy = {
            **self.quote,
            "priceUniverse": "LEGACY_PREPARED_SELECTIONS",
            "priceObservationTimestampVerified": False,
            "independentUnfilteredBookmakerUniverseVerified": False,
            "quoteObservedAt": None,
        }
        report = evaluate_calibration([self.calib], [self.forecast], [legacy])
        self.assertEqual(report["rejected"][
            "UNVERIFIED_INDIVIDUAL_BOOKMAKER_QUOTE_OR_PRICE_UNIVERSE"], 1)
        self.assertEqual(report["marketCounts"], {})
        self.assertEqual(report["oneGoalsScenarioPerFixture"]["n"], 0)

    def test_snapshot_cutoff_does_not_replace_quote_observed_at(self):
        fake = {**self.quote, "quoteObservedAt": self.quote["kickoffUTC"]}
        report = evaluate_calibration([self.calib], [self.forecast], [fake])
        self.assertEqual(report["rejected"][
            "QUOTE_OBSERVED_AFTER_CUTOFF_OR_KICKOFF"], 1)
        self.assertEqual(report["marketCounts"], {})

    def test_nonfinite_probability_rejected(self):
        invalid = {**self.forecast, "probabilities": {
            **self.forecast["probabilities"], "1X2_HOME": float("nan")
        }}
        result = evaluate_calibration([self.calib], [invalid], [self.quote])
        self.assertEqual(result["rejected"][
            "nonfinite_market_probability_or_settlement"], 1)


if __name__ == "__main__":
    unittest.main()
