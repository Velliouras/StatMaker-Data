from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from elo_holdout_diagnostics import summarize_elo_holdout, wilson_lower


def candidate(won: bool, league: str = "E0|Synthetic", p: float = .65) -> dict:
    # Top pick is home. These are retrospective records, not model predictions.
    return {"strategy": "ELO_GOALS_FALLBACK_NO_XG",
            "historicalXgSufficient": False,
            "league": league,
            "probabilities": {"1X2_HOME": p, "1X2_DRAW": .15,
                              "1X2_AWAY": .85-p},
            "observed": {"1X2_HOME": int(won), "1X2_DRAW": 0,
                         "1X2_AWAY": int(not won)}}


class EloResearchHoldoutTests(unittest.TestCase):
    def test_sample_twenty_of_twenty_six_not_certified(self):
        result = summarize_elo_holdout([candidate(i < 20) for i in range(26)])
        high = result["overall"]["rawTop1X2ProbabilityAtLeast60"]
        self.assertEqual((high["n"], high["wins"]), (26, 20))
        self.assertAlmostEqual(high["observedHitRate"], 20 / 26)
        self.assertAlmostEqual(high["wilson95Lower"], .5794807424804321)
        self.assertFalse(high["conservative60Demonstrated"])
        self.assertFalse(high["certified"])
        self.assertTrue(result["notCertified"])

    def test_league_separation(self):
        data = [candidate(True, "E0|Test"), candidate(False, "D1|Test")]
        r = summarize_elo_holdout(data)
        self.assertEqual(r["leagues"]["E0|Test"]["argmaxWins"], 1)
        self.assertEqual(r["leagues"]["D1|Test"]["argmaxWins"], 0)

    def test_no_xg_primary_cross_calibration(self):
        row = candidate(True)
        row["historicalXgSufficient"] = True
        r = summarize_elo_holdout([row])
        self.assertEqual(r["overall"]["n"], 0)
        self.assertEqual(r["rejected"]["not_elo_fallback_only"], 1)

    def test_invalid_distribution_or_outcome_excluded(self):
        row = candidate(True)
        row["probabilities"]["1X2_HOME"] = float("nan")
        self.assertEqual(summarize_elo_holdout([row])["overall"]["n"], 0)
        another = candidate(True)
        another["observed"]["1X2_AWAY"] = 1
        self.assertEqual(summarize_elo_holdout([another])["overall"]["n"], 0)

    def test_empty_has_no_fake_confidence(self):
        r = summarize_elo_holdout([])
        self.assertIsNone(r["overall"]["rawTop1X2ProbabilityAtLeast60"]["wilson95Lower"])
        self.assertEqual(wilson_lower(0, 0), 0.0)
        self.assertEqual(wilson_lower(20, 26), .5794807424804321)


if __name__ == "__main__":
    unittest.main()
