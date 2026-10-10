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
