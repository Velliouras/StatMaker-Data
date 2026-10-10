"""Prematch market inference and retrospective settlement must share identities."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))

from forecast_market import market_probability
from evaluate_holdout_prices import event
from walk_forward_goals import event_probs


class PrematchMarketTests(unittest.TestCase):
    def setUp(self):
        # No observed outcomes here: the engine must predict BEFORE kickoff.
        self.future = {"probabilities": event_probs(2.6, 1.1)}
        self.finished = {**self.future, "homeGoals": 3, "awayGoals": 1}

    def test_future_market_needs_no_final_score(self):
        cases = (
            ("RESULT_1X2", "HOME", "", None, "1X2_HOME"),
            ("RESULT_1X2", "DRAW", "", None, "1X2_DRAW"),
            ("RESULT_1X2", "AWAY", "", None, "1X2_AWAY"),
            ("RESULT_DOUBLE_CHANCE", "HOME_OR_DRAW", "", None,
             "DOUBLE_CHANCE_HOME_OR_DRAW"),
            ("RESULT_DOUBLE_CHANCE", "AWAY_OR_DRAW", "", None,
             "DOUBLE_CHANCE_AWAY_OR_DRAW"),
            ("RESULT_DOUBLE_CHANCE", "HOME_OR_AWAY", "", None,
             "DOUBLE_CHANCE_HOME_OR_AWAY"),
            ("FULL_TIME_MATCH_TOTAL", "OVER", "MATCH", 3.5,
             "MATCH_OVER_3_5_OVER"),
            ("FULL_TIME_MATCH_TOTAL", "UNDER", "MATCH", 3.5,
             "MATCH_OVER_3_5_UNDER"),
            ("HOME_TEAM_TOTAL", "OVER", "HOME", 2.5,
             "HOME_OVER_2_5_OVER"),
            ("AWAY_TEAM_TOTAL", "UNDER", "AWAY", 1.5,
             "AWAY_OVER_1_5_UNDER"),
        )
        for market, direction, team, line, label in cases:
            with self.subTest(market=market, direction=direction):
                result = market_probability({
                    "market": market, "direction": direction,
                    "teamSide": team, "line": line,
                }, self.future)
                self.assertIsNotNone(result)
                self.assertEqual(result[0], label)
                self.assertGreaterEqual(result[1], 0)
                self.assertLessEqual(result[1], 1)

    def test_over_under_probability_complement(self):
        for line in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5):
            for market, team in (("FULL_TIME_MATCH_TOTAL", "MATCH"),
                                 ("HOME_TEAM_TOTAL", "HOME"),
                                 ("AWAY_TEAM_TOTAL", "AWAY")):
                base = {"market": market, "teamSide": team, "line": line}
                over = market_probability({**base, "direction": "OVER"},
                                          self.future)
                under = market_probability({**base, "direction": "UNDER"},
                                           self.future)
                self.assertAlmostEqual(over[1] + under[1], 1.0, places=9)
                self.assertEqual(over[2], 0)
                self.assertEqual(under[2], 0)

    def test_future_predictions_and_settlement_probabilities_match(self):
        for direction in ("OVER", "UNDER"):
            q = {"market": "FULL_TIME_MATCH_TOTAL", "direction": direction,
                 "teamSide": "MATCH", "line": 3.5, "odd": 1.95}
            inference = market_probability(q, self.future)
            historic = event(q, self.finished)
            self.assertEqual(inference, historic[:3])
            self.assertEqual(historic[3], 1.95 if direction == "OVER" else 0.0)

    def test_dnb_is_push_aware_but_not_binary_calibration(self):
        q = {"market": "RESULT_DNB", "direction": "HOME", "odd": 2.2}
        m = market_probability(q, self.future)
        self.assertGreater(m[2], 0)
        draw = {**self.future, "homeGoals": 1, "awayGoals": 1}
        self.assertEqual(event(q, draw)[3], 1.0)

    def test_unsupported_or_asian_lines_do_not_map(self):
        for line in (2.25, 2.75, 3, 6.5, True, None):
            self.assertIsNone(market_probability({
                "market": "FULL_TIME_MATCH_TOTAL", "direction": "OVER",
                "teamSide": "MATCH", "line": line,
            }, self.future))
        for market in ("ASIAN_HANDICAP", "CORRECT_SCORE", "RESULT_HANDICAP"):
            self.assertIsNone(market_probability({
                "market": market, "direction": "HOME", "odd": 1.95,
            }, self.future))

    def test_wrong_team_side_fail_closed(self):
        self.assertIsNone(market_probability({
            "market": "HOME_TEAM_TOTAL", "direction": "OVER",
            "teamSide": "AWAY", "line": 1.5,
        }, self.future))

    def test_inconsistent_1x2_distribution_is_rejected(self):
        corrupted = {"probabilities": dict(self.future["probabilities"])}
        corrupted["probabilities"]["1X2_HOME"] = 0.95
        for market, direction in (
            ("RESULT_1X2", "HOME"),
            ("RESULT_DOUBLE_CHANCE", "HOME_OR_DRAW"),
            ("RESULT_DNB", "HOME"),
        ):
            with self.subTest(market=market):
                with self.assertRaises(ValueError):
                    market_probability({
                        "market": market, "direction": direction,
                    }, corrupted)

    def test_nonfinite_forecast_probability_is_rejected(self):
        corrupted = {"probabilities": dict(self.future["probabilities"])}
        corrupted["probabilities"]["HOME_OVER_1_5"] = float("nan")
        with self.assertRaises(ValueError):
            market_probability({
                "market": "HOME_TEAM_TOTAL", "teamSide": "HOME",
                "direction": "OVER", "line": 1.5,
            }, corrupted)


if __name__ == "__main__":
    unittest.main()
