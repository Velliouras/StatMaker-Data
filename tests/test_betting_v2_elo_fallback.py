"""Strict chronological ELO+goals fallback; never make up xG or certify picks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from walk_forward_elo import EloParams, make_elo_rows, forecasts, lambda_rates


class BettingV2EloFallbackTests(unittest.TestCase):
    @staticmethod
    def fixtures(n=34, *, xg=None):
        first = datetime(2026, 1, 1, 16, tzinfo=timezone.utc)
        return [{
            "group": "E0|Synthetic",
            "fixture_id": str(1000 + i),
            "date": first + timedelta(days=i),
            "home": "A" if i % 2 == 0 else "B",
            "away": "B" if i % 2 == 0 else "A",
            "hg": (2 if i % 3 else 1), "ag": (1 if i % 2 else 0),
            "stats": {"HxG": xg, "AxG": xg},
        } for i in range(n)]

    def test_without_xg_has_elo_forecasts_not_fake_xg(self):
        rows, counts = make_elo_rows(self.fixtures())
        self.assertTrue(rows)
        self.assertTrue(all(r.missing_prior_xg for r in rows))
        self.assertGreater(counts["elo_fallback_due_to_insufficient_xg"], 0)
        p = EloParams(.5, .65, .25)
        f = forecasts(rows, p)
        self.assertTrue(all(x["strategy"] == "ELO_GOALS_FALLBACK_NO_XG" for x in f))
        self.assertTrue(all(x["notCertified"] for x in f))
        self.assertTrue(all(not x["historicalXgSufficient"] for x in f))
        self.assertTrue(all("xg" not in str(k).lower()
                            for k in f[0]["probabilities"].keys()))
        for prediction in f:
            self.assertAlmostEqual(sum(prediction["probabilities"][k]
                                   for k in ("1X2_HOME", "1X2_DRAW", "1X2_AWAY")),
                                   1, places=7)

    def test_complete_xg_routes_to_primary_not_fallback(self):
        rows, counts = make_elo_rows(self.fixtures(xg=1.5))
        self.assertTrue(rows)
        self.assertTrue(all(not r.missing_prior_xg for r in rows))
        self.assertEqual(counts.get("elo_fallback_due_to_insufficient_xg", 0), 0)

    def test_same_day_results_not_used_as_history(self):
        fixtures = self.fixtures(n=25)
        last_date = fixtures[-1]["date"]
        extra = {**fixtures[-1],
                 "fixture_id": "3000", "hg": 15, "ag": 0,
                 "date": last_date + timedelta(hours=1)}
        base, _ = make_elo_rows(fixtures)
        appended, _ = make_elo_rows(fixtures + [extra])
        before = next(row for row in base if row.fixture == fixtures[-1]["fixture_id"])
        after = next(row for row in appended if row.fixture == fixtures[-1]["fixture_id"])
        self.assertEqual(before.attack_h, after.attack_h)
        self.assertEqual(before.elo_delta, after.elo_delta)

    def test_goals_and_ratings_produce_finite_lambdas(self):
        rows, _ = make_elo_rows(self.fixtures())
        for row in rows:
            h, a = lambda_rates(row, EloParams(.5, .65, .5))
            self.assertTrue(.15 <= h <= 4.5)
            self.assertTrue(.15 <= a <= 4.5)


if __name__ == "__main__":
    unittest.main()
