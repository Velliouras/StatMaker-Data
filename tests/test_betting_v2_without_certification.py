"""Direct STRONG model evidence needs backtests, not a certificate/approval.

Synthetic unit fixtures test the contract only; they are never published.
No provider calls, artifacts, workflows or Android changes.
"""
from __future__ import annotations

from copy import deepcopy
import unittest

from scripts.betting_v2.production_app_ready import validate_release
from scripts.betting_v2.production_contract import Blocked


class V2NoCertificationTests(unittest.TestCase):
    def release(self):
        now = 1_800_000_000_000
        criteria = {
            "HIGH": {"minimumWin": .90, "minimumReliability": .80,
                     "maximumIntervalWidth": .12, "minimumComparableCases": 40},
            "CONFIRMED": {"minimumWin": .75, "minimumReliability": .75,
                          "maximumIntervalWidth": .15, "minimumComparableCases": 30},
            "DEVELOPING": {"minimumWin": .60, "minimumReliability": .70,
                           "maximumIntervalWidth": .20, "minimumComparableCases": 20},
        }
        model = {
            "modelVersion": "walk-forward-goals-v2",
            "calibrationVersion": "chronological-calibration-v2",
            "marketModelKey": "RESULT_1X2", "evidenceMode": "ELO_GOALS_FALLBACK_NO_XG",
            "competitionId": "domestic", "leagueCode": "E0", "season": "2026",
            "trainingEndMs": now-9_000_000, "calibrationStartMs": now-8_000_000,
            "calibrationEndMs": now-7_000_000, "holdoutStartMs": now-6_000_000,
            "holdoutEndMs": now-5_000_000, "evaluatedAtMs": now-4_000_000,
            "validUntilMs": now+3600_000, "comparableCases": 100,
            "tierCriteria": criteria, "oddsBandFrom": 1.8, "oddsBandTo": 3.0,
            "outOfTimeValidated": True, "comparableMarketCalibrated": True,
            "independentModelErrorIncluded": True,
            "evidenceModeIndependentlyCalibrated": True,
            "unitStakeRoiLowerBound": -.04, # negative observed CI is not a certificate gate
        }
        f = {
            "contract": "statmaker-betting-v2-forecast-v1", "researchOnly": False,
            "generationId": "real-generation-test", "fixtureId": "game-a",
            "selectionId": "exact-market-selection", "sameScenarioGroup": "home-win",
            "leagueCode": "E0", "season": "2026", "modelVersion": model["modelVersion"],
            "calibrationVersion": model["calibrationVersion"],
            "marketModelKey": model["marketModelKey"], "evidenceMode": model["evidenceMode"],
            "validation": model, "forecastComputedAtMs": now-1000,
            "kickoffEpochMs": now+3_600_000, "quotedOdd": 2.02,
            "conservativeWinProbability": .65, "conservativePushProbability": 0.0,
            "adverseScenarioConservativeWinProbability": .61,
            "adverseScenarioPushProbability": 0.0, "centralWinProbability": .70,
            "calibrationReliability": .82, "conservativeIntervalWidth": .10,
            "tier": "DEVELOPING", "primaryXgUnavailableVerified": True,
            "featureEvidence": [{"feature": name, "sourceSha256": "b"*64,
                "observedAtMs": now-12_000, "validUntilMs": now+240_000}
                for name in ("ELO", "RECENT_FORM", "LONG_TERM_FORM", "HOME_AWAY",
                             "MARKET_SPECIFIC")],
        }
        release = {
            "contract": "statmaker-betting-v2-canonical-release-v2",
            "notYetPublished": True, "producedAtMs": now-1000,
            "generationId": "real-generation-test",
            "forecasts": [{"competitionId": "domestic", "snapshotVersion": "snapshot",
                           "fixtureId": "game-a", "selectionId": "exact-market-selection",
                           "payload": f}],
            "quotes": [], "documents": {},
        }
        return now, release

    def test_direct_evaluated_model_needs_no_certificate_or_approval(self):
        now, release = self.release()
        self.assertNotIn("certificates", release)
        self.assertNotIn("certificationId", release["forecasts"][0]["payload"])
        self.assertNotIn("approval", release)
        validate_release(release, now)

    def test_unvalidated_or_research_probabilities_still_fail(self):
        now, release = self.release()
        for edit in (
            lambda p: p["validation"].update(outOfTimeValidated=False),
            lambda p: p.update(researchOnly=True),
            lambda p: p.update(conservativeWinProbability=.59),
            lambda p: p.update(quotedOdd=3.0),
        ):
            sample = deepcopy(release)
            edit(sample["forecasts"][0]["payload"])
            with self.assertRaises(Blocked):
                validate_release(sample, now)


if __name__ == "__main__":
    unittest.main()
