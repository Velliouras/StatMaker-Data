"""No Actions, no provider calls: verify separate Elo research and safe pilot outputs."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import sys
import tempfile
import unittest

MOD = Path(__file__).resolve().parents[1] / "scripts/betting_v2"
sys.path.insert(0, str(MOD))
from walk_forward_elo import walk_forward_elo
from publish_shadow import _safe_output


class DualModeResearchTests(unittest.TestCase):
    def test_jsonl_allowed_only_via_explicit_research_whitelist(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            research = root / "reports/betting_v2/research.jsonl"
            self.assertRaises(ValueError, _safe_output, root, research)
            self.assertEqual(
                _safe_output(root, research, allowed_suffixes=(".json", ".jsonl")),
                research)
            self.assertRaises(
                ValueError, _safe_output, root,
                root / "data/statmaker/app_ready/production.jsonl",
                allowed_suffixes=(".json", ".jsonl"))

    def test_fallback_tunes_only_on_xg_missing_population(self):
        from walk_forward_elo import EloRow
        rows = []
        start = datetime(2025, 1, 1, 16, tzinfo=timezone.utc)
        for i in range(200):
            rows.append(EloRow(
                date=(start + timedelta(days=i)).date().isoformat(),
                league="E0|Synthetic", fixture=str(i),
                kickoff_utc=(start + timedelta(days=i)).isoformat(),
                hgoals=2, agoals=0,
                attack_h=(1.5, 1.5, 1.5),
                defense_h=(0.9, 0.9, 0.9),
                attack_a=(1.2, 1.2, 1.2),
                defense_a=(1.1, 1.1, 1.1),
                elo_delta=120, league_h=1.4, league_a=1.1,
                missing_prior_xg=(i % 2 == 0),
            ))
        with patch("walk_forward_elo.read_fixtures",
                   return_value=([], Counter(), {"E0|Synthetic": {}})), \
             patch("walk_forward_elo.make_elo_rows", return_value=(rows, {})):
            report, cal, held = walk_forward_elo(Path("."))
        self.assertEqual(report["tuningPopulation"], "ELO_FALLBACK_ONLY_NO_XG")
        self.assertTrue(cal and held)
        self.assertEqual(report["eloFallbackEligible"], 100)
        self.assertEqual(report["splits"]["fallbackTuneN"], report["splits"]["tuneN"])
        self.assertTrue(all(int(x["fixtureId"]) % 2 == 0 for x in cal + held))
        self.assertLess(max(x["date"] for x in cal), min(x["date"] for x in held))

    def test_elo_holdout_is_disjoint_and_not_certified(self):
        start = datetime(2025, 1, 1, 16, tzinfo=timezone.utc)
        fixtures = [{
            "group": "E0|Synthetic",
            "fixture_id": str(2000 + i),
            "date": start + timedelta(days=i),
            "home": "A" if i % 2 else "B",
            "away": "B" if i % 2 else "A",
            "hg": i % 4, "ag": (i + 2) % 4,
            "stats": {"HxG": None, "AxG": None},
        } for i in range(140)]
        with patch("walk_forward_elo.read_fixtures",
                   return_value=(fixtures, Counter(), {"E0|Synthetic": {}})):
            report, cal, held = walk_forward_elo(Path("."))
        self.assertTrue(cal)
        self.assertTrue(held)
        self.assertLess(max(row["date"] for row in cal),
                        min(row["date"] for row in held))
        self.assertTrue(all(row["strategy"] == "ELO_GOALS_FALLBACK_NO_XG"
                            for row in cal + held))
        self.assertTrue(all(row["notCertified"] for row in cal + held))
        self.assertTrue(report["notCertified"])
        self.assertTrue(report["requiresSeparateCalibration"])
        self.assertEqual(report["providerCalls"], 0)
        self.assertEqual(report["liveStrongPicks"], 0)


if __name__ == "__main__":
    unittest.main()
