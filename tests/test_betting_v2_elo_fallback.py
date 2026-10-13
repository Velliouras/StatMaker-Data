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

    def test_first_seen_matches_remain_in_scored_history(self):
        # Two teams alternate venues, so after 20 complete earlier
        # league fixtures both have sufficient scored/venue history.
        # A discarded first-game record would silently undercount readiness.
        rows, reasons = make_elo_rows(self.fixtures(n=34))
        self.assertEqual(len(rows), 14)
        self.assertEqual(rows[0].fixture, "1020")
        self.assertEqual(rows[-1].fixture, "1033")
        self.assertEqual(reasons["elo_fallback_due_to_insufficient_xg"], 14)

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



class BettingV2ProductionMixedXgTests(unittest.TestCase):
    """The real fitted-model feature builder cannot require total xG absence."""

    @staticmethod
    def setup(matches_with_missing_xg: bool):
        from scripts.betting_v2.production_models import features
        base = 1730000000000
        day = 86400000
        history = []
        for i in range(34):
            hg, ag = 2, 1
            missing = matches_with_missing_xg and i == 31
            row = {
                "leagueCode": "E0", "fixture_id": str(i+1000),
                "home_team_id": 1 if i % 2 == 0 else 2,
                "away_team_id": 2 if i % 2 == 0 else 1,
                "kickoffEpochMs": base+i*day,
                "availableAtMs": base+i*day+3*3600000,
                "season": "2025", "goals": [hg,ag],
                "counts": {"GOALS_FT":[hg,ag]},
                "providerXgUnavailable": missing,
            }
            if not missing:
                row["xg"] = [1.6, 0.9]
            history.append(row)
        at = base+35*day
        target = {"fixture_id":"UPCOMING", "home_team_id":1,
                  "away_team_id":2,"leagueCode":"E0",
                  "season":"2025", "kickoffEpochMs":at+3600000}
        params = {"eloScale":400,"eloK":18,"minimumHistory":3,
                  "recentWindow":5,"longWindow":10,"venueWindow":6}
        return features, history, target, at, params

    def test_partial_xg_requires_elo_only_not_fake_xg(self):
        from scripts.betting_v2.production_contract import Blocked
        features, history, target, at, params = self.setup(True)
        vector, used, eligible = features(
            history,target,at,"GOALS_FT","ELO_GOALS_FALLBACK_NO_XG",
            params,None,[])
        self.assertGreater(len(vector),30)
        self.assertTrue(used and eligible)
        with self.assertRaises(Blocked):
            features(history,target,at,"GOALS_FT","XG_PRIMARY",
                     params,None,[])

    def test_complete_xg_forbids_fallback_and_allows_primary(self):
        from scripts.betting_v2.production_contract import Blocked
        features, history, target, at, params = self.setup(False)
        p, used, _ = features(history,target,at,"GOALS_FT","XG_PRIMARY",
                              params,None,[])
        self.assertTrue(p and used)
        with self.assertRaises(Blocked):
            features(history,target,at,"GOALS_FT","ELO_GOALS_FALLBACK_NO_XG",
                     params,None,[])

    def test_unverified_missing_xg_cannot_be_accepted(self):
        from scripts.betting_v2.production_contract import Blocked
        features, history, target, at, params = self.setup(True)
        history[31]["providerXgUnavailable"] = False
        with self.assertRaises(Blocked):
            features(history,target,at,"GOALS_FT","ELO_GOALS_FALLBACK_NO_XG",
                     params,None,[])

    def test_nonempty_injury_context_requires_actual_provider_record(self):
        from scripts.betting_v2.production_contract import Blocked
        features, history, target, at, params = self.setup(True)
        with self.assertRaises(Blocked):
            features(history,target,at,"GOALS_FT","ELO_GOALS_FALLBACK_NO_XG",
                     params,None,["verifiedInjuryCount"])


if __name__ == "__main__":
    unittest.main()
