import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import statmaker_domestic_scope as scope


class StatMakerDomesticScopeTest(unittest.TestCase):
    def test_stats_universe_contains_exactly_fifty_three_leagues(self):
        self.assertEqual(53, len(scope.stats_universe_codes()))

    def test_core_odds_scope_remains_exactly_twenty_seven_leagues(self):
        self.assertEqual(27, len(scope.included_codes()))
        self.assertEqual(scope.included_codes(), scope.core_odds_codes())

    def test_absolute_priority_is_main_five_plus_greece(self):
        self.assertEqual(
            {"E0", "D1", "I1", "SP1", "F1", "G1"},
            scope.absolute_priority_codes(),
        )

    def test_romania_code_is_normalized_in_both_scopes(self):
        self.assertTrue(scope.is_included("ROM"))
        self.assertTrue(scope.is_included("ROU"))
        self.assertTrue(scope.is_stats_included("ROM"))
        self.assertTrue(scope.is_stats_included("ROU"))

    def test_restored_leagues_are_stats_only_until_dynamic_odds_expansion(self):
        for code in ("CZE", "SRB", "USA", "JPN", "BRA2", "AUT2", "SWE2"):
            self.assertTrue(scope.is_stats_included(code), code)
            self.assertFalse(scope.is_included(code), code)
            self.assertEqual(2, scope.priority_rank(code))

    def test_priority_tiers(self):
        self.assertEqual(0, scope.priority_rank("E0"))
        self.assertEqual(0, scope.priority_rank("G1"))
        self.assertEqual(1, scope.priority_rank("E1"))
        self.assertEqual(2, scope.priority_rank("CZE"))

    def test_core_registry_filter_keeps_only_core_odds_scope(self):
        payload = {
            "leagueCount": 4,
            "leagues": [
                {"leagueCode": "E0"},
                {"leagueCode": "G1"},
                {"leagueCode": "CZE"},
                {"leagueCode": "USA"},
            ],
        }
        filtered = scope.filter_registry_payload(payload)
        self.assertEqual(["E0", "G1"], [row["leagueCode"] for row in filtered["leagues"]])
        self.assertEqual(2, filtered["leagueCount"])
        self.assertEqual("core_odds", filtered["domesticScope"]["scopeType"])

    def test_stats_registry_filter_keeps_restored_leagues(self):
        payload = {
            "leagueCount": 4,
            "leagues": [
                {"leagueCode": "E0"},
                {"leagueCode": "G1"},
                {"leagueCode": "CZE"},
                {"leagueCode": "USA"},
            ],
        }
        filtered = scope.filter_stats_registry_payload(payload)
        self.assertEqual(
            ["E0", "G1", "CZE", "USA"],
            [row["leagueCode"] for row in filtered["leagues"]],
        )
        self.assertEqual(4, filtered["leagueCount"])
        self.assertEqual("stats_universe", filtered["domesticScope"]["scopeType"])


if __name__ == "__main__":
    unittest.main()
