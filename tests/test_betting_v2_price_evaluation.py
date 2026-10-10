"""Exact historical odds settlement tests, synthetic and zero provider requests."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from evaluate_holdout_prices import evaluate


class BettingV2HistoricalSettlementTests(unittest.TestCase):
    def setUp(self):
        self.forecast = {
            "league": "E0|Example", "leagueCode": "E0", "fixtureId": "90001",
            "date": "2026-10-10",
            "kickoffUTC": "2026-10-10T22:30:00+00:00",
            "homeGoals": 2, "awayGoals": 1,
            "probabilities": {
                "1X2_HOME": .61, "1X2_DRAW": .23, "1X2_AWAY": .16,
                "HOME_OVER_1_5": .64, "AWAY_OVER_1_5": .28,
                "MATCH_OVER_2_5": .58
            },
            "observed": {
                "1X2_HOME": 1, "1X2_DRAW": 0, "1X2_AWAY": 0,
                "HOME_OVER_1_5": 1, "AWAY_OVER_1_5": 0,
                "MATCH_OVER_2_5": 1
            }
        }
        self.base = {
            "leagueCode": "E0", "fixtureId": "90001",
            "date": "2026-10-11",  # Athens day, after midnight
            "kickoffUTC": "2026-10-10T22:30:00+00:00",
            "quoteCutoff": "2026-10-11T00:00:00+03:00",
            "identityResolution": "EXACT_CACHED_FIXTURE",
            "selectionKey": "home1", "market": "RESULT_1X2",
            "direction": "HOME", "odd": 2.10,
            "teamSide": None, "line": None
        }

    def test_athens_rollover_and_correct_1x2_gross_return(self):
        report = evaluate([self.forecast], [self.base])
        self.assertEqual(report["availableExactJoinedQuotes"], 1)
        selected = report["oneCorrelatedScenarioPerMatch"]
        self.assertEqual(selected["n"], 1)
        self.assertEqual(selected["won"], 1)
        self.assertAlmostEqual(selected["roi"], 1.10)

    def test_dnb_draw_is_push_and_does_not_count_as_win(self):
        draw = dict(self.forecast)
        draw["homeGoals"], draw["awayGoals"] = 1, 1
        quote = {**self.base, "selectionKey": "homeDnb",
                 "market": "RESULT_DNB", "odd": 1.90}
        report = evaluate([draw], [quote])
        self.assertEqual(report["allResearchQuotes"]["push"], 1)
        self.assertEqual(report["allResearchQuotes"]["won"], 0)
        self.assertAlmostEqual(report["allResearchQuotes"]["roi"], 0.0)

    def test_high_price_rejected_even_on_good_outcome(self):
        r = evaluate([self.forecast], [{**self.base, "odd": 7.0}])
        self.assertEqual(r["availableExactJoinedQuotes"], 0)
        self.assertEqual(r["rejected"]["outside_odds_contract"], 1)

    def test_home_team_goal_over_1_5_settles_correctly(self):
        quote = {**self.base, "selectionKey": "homeGoals",
                 "market": "HOME_TEAM_TOTAL", "direction": "OVER",
                 "line": 1.5, "teamSide": "HOME", "odd": 2.0}
        r = evaluate([self.forecast], [quote])
        self.assertEqual(r["rawProbabilityAndValueCandidates"]["n"], 1)
        self.assertEqual(r["oneCorrelatedScenarioPerMatch"]["won"], 1)

    def test_changed_kickoff_never_falsely_joins(self):
        quote = {**self.base, "kickoffUTC": "2026-10-10T18:30:00+00:00"}
        r = evaluate([self.forecast], [quote])
        self.assertEqual(r["availableExactJoinedQuotes"], 0)
        self.assertEqual(r["rejected"]["KICKOFF_MISMATCH"], 1)


if __name__ == "__main__":
    unittest.main()
