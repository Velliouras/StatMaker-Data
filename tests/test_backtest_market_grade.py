import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backtest_elo_betting_ab as audit


class BacktestMarketGradeTest(unittest.TestCase):
    def test_odd_even_uses_selection_name_because_side_is_unknown(self):
        baseline = {
            "selectionName": "Odd", "selectionSide": "UNKNOWN",
            "subMarketKey": "GOALS_ODD_EVEN", "odd": 1.91
        }
        stats = {"status": "FT", "homeGoals": 3, "awayGoals": 2}
        self.assertEqual(audit.grade_pick(baseline, stats), ("WON", 1.91))
        self.assertEqual(
            audit.grade_pick(dict(baseline, selectionName="Even"), stats),
            ("LOST", 0.0),
        )
        stats["awayGoals"] = 1
        self.assertEqual(
            audit.grade_pick(dict(baseline, selectionName="Even"), stats),
            ("WON", 1.91),
        )

    def test_unrecognized_odd_even_identity_remains_ungraded(self):
        pick = {"selectionName": "?", "selectionSide": "UNKNOWN",
                "subMarketKey": "GOALS_ODD_EVEN", "odd": 2.1}
        self.assertEqual(
            audit.grade_pick(pick, {"status": "FT", "homeGoals": 1, "awayGoals": 1}),
            (None, None),
        )

    def test_pending_does_not_get_fabricated_result(self):
        pick = {"selectionName": "Even", "subMarketKey": "GOALS_ODD_EVEN", "odd": 2.0}
        self.assertEqual(
            audit.grade_pick(pick, {"status": "NS", "homeGoals": 0, "awayGoals": 0}),
            (None, None),
        )


if __name__ == "__main__":
    unittest.main()
