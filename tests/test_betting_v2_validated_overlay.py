"""Strict read-only recovered-xG validation and point-in-time embargo."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from validate_xg_overlay import inspect_overlay

SHA = "a" * 40


def sample():
    original = {"competition": {"league_code": "E0"}, "matches": [{
        "fixture_id": 123, "status": "FT",
        "date_utc": "2026-09-05T18:00:00Z",
        "home_team": "Arsenal", "away_team": "Coventry",
        "home_goals": 3, "away_goals": 0,
        "normalized_stats": {"HxG": None, "AxG": None},
    }]}
    overlay = {
        "contract": "betting-v2-understat-xg-research-overlay-v1",
        "certified": False, "historicalAsOfVerified": False,
        "canonicalDataModified": False, "mayBackfillTrainingHistory": False,
        "mayCertifyPicks": False, "directUnderstatVerified": False,
        "sourceModelsMayDiffer": True,
        "leagueCode": "E0", "source": "THIRD_PARTY_UNDERSTAT_MIRROR",
        "sourceCommit": SHA, "sourceBlobSha": SHA,
        "sourceCommitTimestampUTC": "2026-10-06T20:59:42Z",
        "sourceObservedAtUTC": "2026-10-06T20:59:42Z",
        "recovered": [{
            "fixtureId": "123", "understatMatchId": "31180",
            "leagueCode": "E0", "kickoffUTC": "2026-09-05T18:00:00Z",
            "homeTeam": "Arsenal", "awayTeam": "Coventry",
            "homeGoals": 3, "awayGoals": 0,
            "homeXg": 1.85424, "awayXg": 0.558336,
            "historicalAsOfVerified": False,
        }],
    }
    return overlay, original


class RecoveredXgOverlayTests(unittest.TestCase):
    def test_valid_canonical_match_remains_uncertified(self):
        overlay, original = sample()
        r = inspect_overlay(overlay, original)
        self.assertEqual(r["validResearchOverlayRows"], 1)
        self.assertTrue(r["allRowsPassed"])
        self.assertFalse(r["canPublishStrong"])
        self.assertFalse(r["readyForHistoricalBacktestBeforeSourceCommit"])
        self.assertFalse(r["canMixDifferentXgModelsWithoutRecalibration"])

    def test_existing_conflicting_xg_is_not_overwritten(self):
        overlay, original = sample()
        original["matches"][0]["normalized_stats"]["HxG"] = 1.1
        r = inspect_overlay(overlay, original)
        self.assertEqual(r["validResearchOverlayRows"], 0)
        self.assertEqual(r["rejectedByReason"]["would_overwrite_canonical_home_xg"], 1)

    def test_future_result_cannot_be_in_old_archive(self):
        overlay, original = sample()
        overlay["sourceCommitTimestampUTC"] = "2026-08-30T00:00:00Z"
        overlay["sourceObservedAtUTC"] = "2026-08-30T00:00:00Z"
        r = inspect_overlay(overlay, original)
        self.assertEqual(
            r["rejectedByReason"]["kickoff_mismatch_or_not_before_source_commit"], 1)

    def test_score_or_team_mismatch_excluded(self):
        overlay, original = sample()
        overlay["recovered"][0]["homeGoals"] = 4
        r = inspect_overlay(overlay, original)
        self.assertEqual(r["rejectedByReason"]["team_or_score_mismatch"], 1)

    def test_duplicate_source_match_id_excluded(self):
        overlay, original = sample()
        original["matches"].append({
            **original["matches"][0], "fixture_id": 124
        })
        overlay["recovered"].append({
            **overlay["recovered"][0], "fixtureId": "124"
        })
        r = inspect_overlay(overlay, original)
        self.assertEqual(r["validResearchOverlayRows"], 1)
        self.assertEqual(r["rejectedByReason"]["duplicate_or_missing_identifier"], 1)

    def test_false_asof_or_certification_is_rejected(self):
        overlay, original = sample()
        overlay["certified"] = True
        with self.assertRaises(ValueError):
            inspect_overlay(overlay, original)
        overlay["certified"] = False
        overlay["historicalAsOfVerified"] = True
        with self.assertRaises(ValueError):
            inspect_overlay(overlay, original)

    def test_timezone_required_and_observed_after_commit(self):
        overlay, original = sample()
        overlay["sourceCommitTimestampUTC"] = "2026-10-06T20:59:42"
        with self.assertRaises(ValueError):
            inspect_overlay(overlay, original)
        overlay["sourceCommitTimestampUTC"] = "2026-10-07T00:00:00Z"
        with self.assertRaises(ValueError):
            inspect_overlay(overlay, original)


if __name__ == "__main__":
    unittest.main()
