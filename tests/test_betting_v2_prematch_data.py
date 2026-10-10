"""Synthetic no-API research tests: no silent xG/xGA imputation.

Run explicitly:
 python -m unittest tests/test_betting_v2_prematch_data.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

MOD = Path(__file__).resolve().parents[1] / "scripts/betting_v2"
sys.path.insert(0, str(MOD))
from walk_forward_goals import make_rows


class BettingV2PrematchDataTests(unittest.TestCase):
    def matches(self, missing_recent_xg: bool) -> list[dict]:
        first = datetime(2025, 1, 1, 16, tzinfo=timezone.utc)
        data = []
        for i in range(23):
            home = "A" if i % 2 == 0 else "B"
            away = "B" if i % 2 == 0 else "A"
            stats = {"HxG": 1.6, "AxG": 1.1}
            if missing_recent_xg and i == 21:
                stats["HxG"] = None
            data.append({
                "group": "E0|Synthetic",
                "fixture_id": str(1000 + i),
                "date": first + timedelta(days=i),
                "home": home, "away": away,
                "hg": 2, "ag": 1,
                "stats": stats,
            })
        return data

    def test_current_fixture_with_complete_prematch_xg_can_be_audited(self):
        rows, counts = make_rows(self.matches(missing_recent_xg=False))
        last = [r for r in rows if r.fixture == "1022"]
        self.assertEqual(len(last), 1)
        self.assertEqual(last[0].kickoff_utc,
                         "2025-01-23T16:00:00+00:00")

    def test_missing_recent_attack_or_defense_xg_excludes_fixture(self):
        rows, counts = make_rows(self.matches(missing_recent_xg=True))
        self.assertNotIn("1022", {r.fixture for r in rows})
        self.assertGreaterEqual(counts.get("missing_recent5_xg_xga", 0), 1)

    def test_stale_lifetime_venue_history_cannot_pass_last20_window(self):
        first = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        data = []
        for index in range(43):
            # A has eight+ historic HOME fixtures, B eight+ AWAY fixtures,
            # but their latest twenty matches have the opposite venues only.
            a_home = index < 15 or index == 42
            data.append({
                "group": "E0|Synthetic", "fixture_id": str(2000 + index),
                "date": first + timedelta(days=index),
                "home": "A" if a_home else "B",
                "away": "B" if a_home else "A",
                "hg": 2, "ag": 1,
                "stats": {"HxG": 1.8, "AxG": 1.2},
            })
        rows, reasons = make_rows(data)
        # Previously this would raise ValueError from the empty venue window
        # despite the lifetime venue readiness gate having passed.
        self.assertNotIn("2042", {row.fixture for row in rows})
        self.assertGreater(reasons.get("insufficient_venue_xg_xga", 0), 0)

    def test_model_does_not_substitute_default_xg(self):
        from inspect import getsource
        source = getsource(__import__("walk_forward_goals").make_rows)
        self.assertNotIn('fallback = 1.4', source)
        self.assertIn('missing_recent5_xg_xga', source)


if __name__ == "__main__":
    unittest.main()
