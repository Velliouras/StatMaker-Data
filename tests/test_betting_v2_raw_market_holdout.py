"""Regression tests for independent, strictly unpriced V2 market holdouts."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "betting_v2"))

from raw_market_holdout import summarize
from walk_forward_goals import event_probs, outcomes


def forecast(day: str, fixture: str, *, elo: bool = False) -> dict:
    row = {
        "date": day,
        "league": "E0|Premier League",
        "leagueCode": "E0",
        "fixtureId": fixture,
        "kickoffUTC": day + "T15:00:00+00:00",
        "homeGoals": 3,
        "awayGoals": 1,
        "probabilities": event_probs(2.1, 1.0),
        "observed": outcomes(SimpleNamespace(hgoals=3, agoals=1)),
    }
    if elo:
        row["strategy"] = "ELO_GOALS_FALLBACK_NO_XG"
        row["historicalXgSufficient"] = False
    return row


class UnpricedHoldoutTests(unittest.TestCase):
    def test_primary_contains_all_goal_lines_but_zero_certificates(self):
        report = summarize(
            [forecast("2026-09-01", "cal")],
            [forecast("2026-09-05", "held")], "XG_PRIMARY"
        )
        self.assertFalse(report["certified"])
        self.assertEqual(report["strongRecommendations"], 0)
        self.assertFalse(report["hasVerifiedIndependentBookmakerPrices"])
        self.assertFalse(report["hasROIProof"])
        self.assertIn("MATCH_OVER_3_5_OVER", report["byMarket"])
        self.assertIn("MATCH_OVER_3_5_UNDER", report["byMarket"])
        self.assertIn("HOME_OVER_2_5_OVER", report["byMarket"])
        self.assertIn("AWAY_OVER_4_5_UNDER", report["byMarket"])
        self.assertIn("DOUBLE_CHANCE_HOME_OR_DRAW", report["byMarket"])
        self.assertEqual(report["byMarket"]["MATCH_OVER_3_5_OVER"]["n"], 1)

    def test_one_calibration_fixture_cannot_certify_any_market(self):
        report = summarize(
            [forecast("2026-09-01", "cal")],
            [forecast("2026-09-05", "held")], "XG_PRIMARY"
        )
        gate = report["frozenCalibrationReadiness"]
        self.assertEqual(gate["passedMarketEvaluations"], 0)
        self.assertEqual(gate["fixturesWithAtLeastOnePassingMarket"], 0)
        self.assertGreater(gate["blockedCounts"]["insufficient_global_market_bin"], 0)
        self.assertEqual(gate["certifiedStrong"], 0)

    def test_90_earlier_observations_can_pass_unpriced_gate_not_strong(self):
        calibration = [
            forecast("2026-09-01", f"historical-{i}")
            for i in range(90)
        ]
        report = summarize(calibration,
                           [forecast("2026-09-05", "held")],
                           "XG_PRIMARY")
        gate = report["frozenCalibrationReadiness"]
        self.assertGreater(gate["passedMarketEvaluations"], 0)
        self.assertEqual(gate["fixturesWithAtLeastOnePassingMarket"], 1)
        self.assertGreater(gate["passedByMarket"]["1X2_HOME"], 0)
        self.assertFalse(gate["hasBookmakerQuoteOrExpectedValueEvidence"])
        self.assertFalse(gate["hasAdverseLineupBounds"])
        self.assertEqual(gate["certifiedStrong"], 0)
        self.assertFalse(report["certified"])

    def test_elo_population_is_independent(self):
        mode = "ELO_GOALS_FALLBACK_NO_XG"
        report = summarize(
            [forecast("2026-09-01", "cal", elo=True)],
            [forecast("2026-09-05", "held", elo=True)], mode
        )
        self.assertEqual(report["modelPopulation"], mode)
        self.assertFalse(report["certified"])
        with self.assertRaises(ValueError):
            summarize(
                [forecast("2026-09-01", "cal")],
                [forecast("2026-09-05", "held", elo=True)], mode
            )

    def test_chronological_overlap_is_rejected(self):
        with self.assertRaises(ValueError):
            summarize(
                [forecast("2026-09-05", "cal")],
                [forecast("2026-09-05", "held")], "XG_PRIMARY"
            )

    def test_fallback_cannot_enter_primary_calibration(self):
        with self.assertRaises(ValueError):
            summarize(
                [forecast("2026-09-01", "cal", elo=True)],
                [forecast("2026-09-05", "held")], "XG_PRIMARY"
            )

    def test_duplicate_fixture_counts_once(self):
        report = summarize(
            [forecast("2026-09-01", "cal")],
            [forecast("2026-09-05", "held")] * 2, "XG_PRIMARY"
        )
        self.assertEqual(report["rejected"]["duplicate_fixture"], 1)
        self.assertEqual(report["byMarket"]["1X2_HOME"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
