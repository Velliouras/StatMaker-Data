"""Betting V2: symmetric research goal lines, settlement and ELO/xG parity.

Offline stdlib tests. Neither Action/provider calls nor production publishing.
Run: python -m unittest discover -s tests -p 'test_betting_v2_goal_line_contract.py' -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "betting_v2"))

from walk_forward_goals import GOAL_LINES, event_probs, outcomes
from walk_forward_elo import EloRow, EloParams, forecasts
from calibration_gate import model_events
from evaluate_holdout_prices import event


class GoalLineResearchContractTest(unittest.TestCase):
    def setUp(self):
        self.p = event_probs(2.4, 1.2)
        self.result = outcomes(SimpleNamespace(hgoals=3, agoals=1))
        self.forecast = {
            "probabilities": self.p,
            "observed": self.result,
            "homeGoals": 3,
            "awayGoals": 1,
        }

    def test_all_lines_and_three_results_present(self):
        self.assertEqual(GOAL_LINES, (0.5, 1.5, 2.5, 3.5, 4.5, 5.5))
        self.assertAlmostEqual(sum(self.p[f"1X2_{outcome}"]
                                   for outcome in ("HOME", "DRAW", "AWAY")), 1.0, places=9)
        for prefix in ("HOME", "AWAY", "MATCH"):
            previous = 1.0
            for line in GOAL_LINES:
                key = f"{prefix}_OVER_{int(line)}_5"
                self.assertIn(key, self.p)
                self.assertIn(key, self.result)
                self.assertGreaterEqual(self.p[key], 0.0)
                self.assertLessEqual(self.p[key], 1.0)
                self.assertLessEqual(self.p[key], previous)
                previous = self.p[key]

    def test_real_observed_score_sets_correct_binary_labels(self):
        self.assertEqual(self.result["HOME_OVER_2_5"], 1)
        self.assertEqual(self.result["HOME_OVER_3_5"], 0)
        self.assertEqual(self.result["AWAY_OVER_0_5"], 1)
        self.assertEqual(self.result["AWAY_OVER_1_5"], 0)
        self.assertEqual(self.result["MATCH_OVER_3_5"], 1)
        self.assertEqual(self.result["MATCH_OVER_4_5"], 0)

    def test_over_and_under_calibration_complements(self):
        rows = model_events(self.forecast)
        lookup = {market: (prob, label) for market, prob, label in rows}
        for side in ("HOME", "AWAY", "MATCH"):
            for line in GOAL_LINES:
                root = f"{side}_OVER_{int(line)}_5"
                over_p, over_y = lookup[root + "_OVER"]
                under_p, under_y = lookup[root + "_UNDER"]
                self.assertAlmostEqual(over_p + under_p, 1.0, places=9)
                self.assertEqual(over_y + under_y, 1)
        self.assertEqual(len(rows), 3 + 3 * len(GOAL_LINES) * 2 + 3)

    def test_exact_line_settlement_and_invalid_lines(self):
        samples = (
            ("FULL_TIME_MATCH_TOTAL", "OVER", "MATCH", 3.5, 4, 1),
            ("FULL_TIME_MATCH_TOTAL", "UNDER", "MATCH", 4.5, 4, 1),
            ("HOME_TEAM_TOTAL", "OVER", "HOME", 2.5, 3, 1),
            ("AWAY_TEAM_TOTAL", "UNDER", "AWAY", 1.5, 1, 1),
            ("AWAY_TEAM_TOTAL", "OVER", "AWAY", 1.5, 1, 0),
        )
        for market, direction, side, line, actual_goals, win in samples:
            with self.subTest(market=market, direction=direction, line=line):
                parsed = event({
                    "market": market, "direction": direction, "teamSide": side,
                    "line": line, "odd": 2.0,
                }, self.forecast)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed[2], 0.0)
                self.assertEqual(parsed[3], 2.0 if win else 0.0)
        for line in (3.25, 6.5, True, None):
            self.assertIsNone(event({
                "market": "FULL_TIME_MATCH_TOTAL", "direction": "OVER",
                "teamSide": "MATCH", "line": line, "odd": 2.0
            }, self.forecast))
        self.assertIsNone(event({
            "market": "HOME_TEAM_TOTAL", "direction": "OVER",
            "teamSide": "AWAY", "line": 2.5, "odd": 2.0
        }, self.forecast))

    def test_elo_fallback_has_same_labels_without_fake_xg(self):
        sample = EloRow(
            date="2026-09-20", league="E0|Premier League", fixture="fixture-1",
            kickoff_utc="2026-09-20T16:00:00+00:00", hgoals=3, agoals=1,
            attack_h=(1.6, 1.6, 1.6), defense_h=(1.0, 1.0, 1.0),
            attack_a=(1.1, 1.1, 1.1), defense_a=(1.8, 1.8, 1.8),
            elo_delta=80.0, league_h=1.4, league_a=1.2, missing_prior_xg=True
        )
        output = forecasts([sample], EloParams(recent=0.5, venue=0.65, elo=0.5))[0]
        self.assertEqual(output["strategy"], "ELO_GOALS_FALLBACK_NO_XG")
        self.assertIs(output["historicalXgSufficient"], False)
        self.assertEqual(set(output["probabilities"]), set(self.p))
        self.assertEqual(output["observed"], self.result)
        self.assertTrue(output["notCertified"])


if __name__ == "__main__":
    unittest.main()
