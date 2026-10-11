"""Offline QA artifact audit must never infer Strong/EV from holdout score alone."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from audit_real_holdout_artifact import assess, MODES, FOCUS


def actual_like():
    return {
        "pilot_market_holdout.json": {
            "contract": "betting-v2-unpriced-market-holdout-v1",
            "modelPopulation": "XG_PRIMARY", "certified": False, "researchOnly": True,
            "byMarket": {"1X2_HOME": {"rawProbabilityAtLeast60": {
                "n": 25, "wins": 24, "wilson95Lower": 0.80455, "observedHitRate": 0.96
            }}},
        },
        "pilot_elo_market_holdout.json": {
            "contract": "betting-v2-unpriced-market-holdout-v1",
            "modelPopulation": "ELO_GOALS_FALLBACK_NO_XG",
            "certified": False, "researchOnly": True,
            "byMarket": {"1X2_HOME": {"rawProbabilityAtLeast60": {
                "n": 200, "wins": 133, "wilson95Lower": 0.59702,
                "observedHitRate": 0.665
            }}},
        },
        "pilot_cached_odds_provenance.json": {
            "contract": "betting-v2-cached-odds-structural-audit-v1",
            "researchOnly": True, "priceObservationTimestampVerified": False,
            "independentUnfilteredBookmakerUniverseVerified": False,
            "quoteIdentityAndAsOfProvenanceIndependentlyVerified": False,
        },
    }


class RealHoldoutAuditTests(unittest.TestCase):
    def test_independent_population_and_no_certification(self):
        result = assess(actual_like())
        self.assertEqual(result["certifiedStrong"], 0)
        self.assertFalse(result["bookmakerTimestampVerified"])
        self.assertEqual(set(result["populationReports"]), set(MODES.values()))
        self.assertEqual(
            result["populationReports"]["ELO_GOALS_FALLBACK_NO_XG"]
                ["focusMarkets"][0]["wilson95Lower"], 0.59702
        )
        self.assertFalse(result["populationReports"]["XG_PRIMARY"]["quotedROIProven"])

    def test_reject_mislabeled_model(self):
        bad = actual_like()
        bad["pilot_elo_market_holdout.json"]["modelPopulation"] = "XG_PRIMARY"
        with self.assertRaisesRegex(ValueError, "Mixed"):
            assess(bad)

    def test_reject_fake_certified_inputs(self):
        bad = actual_like()
        bad["pilot_market_holdout.json"]["certified"] = True
        with self.assertRaisesRegex(ValueError, "non-research"):
            assess(bad)

    def test_reject_corrupt_real_outcomes(self):
        bad = actual_like()
        bad["pilot_market_holdout.json"]["byMarket"]["1X2_HOME"][
            "rawProbabilityAtLeast60"]["wins"] = 30
        with self.assertRaisesRegex(ValueError, "Invalid"):
            assess(bad)

    def test_reject_wrong_quote_contract(self):
        bad = actual_like()
        bad["pilot_cached_odds_provenance.json"]["contract"] = "unknown"
        with self.assertRaisesRegex(ValueError, "unknown"):
            assess(bad)


if __name__ == "__main__":
    unittest.main()
