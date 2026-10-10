"""Strict independent Understat xG recovery: never synthesize or overwrite."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from recover_understat_xg import recovery_rows, provider_fixture_rows

OBSERVED = "2026-10-10T10:00:00+00:00"


def canonical(hxg=None, axg=None, home="Arsenal", away="Coventry"):
    return {
        "competition": {"league_code": "E0"},
        "matches": [{
            "fixture_id": 123, "status": "FT", "date_utc": "2026-09-05T18:00:00Z",
            "home_team": home, "away_team": away,
            "home_goals": 3, "away_goals": 1,
            "normalized_stats": {"HxG": hxg, "AxG": axg},
        }]
    }


def season(hxg="2.40", axg="0.70", home="Arsenal", away="Coventry",
           score_h="3", score_a="1", timestamp="2026-09-05 18:00:00"):
    return {"dates": [{
        "id": "understat-999", "isResult": True,
        "h": {"title": home}, "a": {"title": away},
        "goals": {"h": score_h, "a": score_a},
        "xG": {"h": hxg, "a": axg},
        "datetime": timestamp,
    }]}


class RecoveryTests(unittest.TestCase):
    def test_exact_provenance_recovery_only_to_research_overlay(self):
        result = recovery_rows(canonical(), season(), "E0", OBSERVED)
        self.assertEqual(len(result["recovered"]), 1)
        item = result["recovered"][0]
        self.assertEqual(item["homeXg"], 2.4)
        self.assertEqual(item["awayXg"], .7)
        self.assertFalse(item["historicalAsOfVerified"])
        self.assertEqual(result["apiFootballCalls"], 0)
        self.assertFalse(result["certified"])

    def test_wrong_score_rejected(self):
        result = recovery_rows(canonical(), season(score_h="2"), "E0", OBSERVED)
        self.assertFalse(result["recovered"])

    def test_wrong_team_rejected_and_explicit_alias_supported(self):
        understat = season(home="Arsenal FC")
        self.assertFalse(recovery_rows(canonical(), understat, "E0", OBSERVED)["recovered"])
        fixed = recovery_rows(canonical(), understat, "E0", OBSERVED,
                              aliases={"Arsenal": "Arsenal FC"})
        self.assertEqual(len(fixed["recovered"]), 1)

    def test_wrong_date_rejected(self):
        bad = season(timestamp="2026-09-07 18:00:00")
        self.assertFalse(recovery_rows(canonical(), bad, "E0", OBSERVED)["recovered"])

    def test_missing_xg_never_imputed(self):
        src = season(hxg=None)
        self.assertFalse(recovery_rows(canonical(), src, "E0", OBSERVED)["recovered"])

    def test_conflicting_existing_xg_not_overwritten(self):
        out = recovery_rows(canonical(hxg=1.5), season(), "E0", OBSERVED)
        self.assertFalse(out["recovered"])
        self.assertEqual(out["counts"]["provider_xg_conflict"], 1)

    def test_duplicate_understat_candidates_ambiguous(self):
        source = season()
        source["dates"].append(dict(source["dates"][0]))
        self.assertFalse(recovery_rows(canonical(), source, "E0", OBSERVED)["recovered"])

    def test_unverified_observation_timezone_rejected(self):
        with self.assertRaises(ValueError):
            recovery_rows(canonical(), season(), "E0", "2026-10-10T10:00:00")

    def test_different_league_rejected(self):
        with self.assertRaises(ValueError):
            recovery_rows(canonical(), season(), "D1", OBSERVED)


if __name__ == "__main__":
    unittest.main()
