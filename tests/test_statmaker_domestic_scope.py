import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import statmaker_domestic_scope as scope


EXPANSION_CODES = {
    "CRO", "CYP", "ISR", "UKR", "AUS", "KOR", "SAU", "UAE",
    "COL", "CHL", "URU", "PER", "ECU", "EGY", "MAR", "RSA",
}


class StatMakerDomesticScopeTest(unittest.TestCase):
    def test_stats_universe_contains_exactly_sixty_nine_leagues(self):
        self.assertEqual(69, len(scope.stats_universe_codes()))

    def test_odds_universe_contains_exactly_sixty_nine_leagues(self):
        self.assertEqual(69, len(scope.odds_universe_codes()))
        self.assertEqual(scope.stats_universe_codes(), scope.odds_universe_codes())

    def test_core_odds_scope_remains_exactly_twenty_seven_priority_leagues(self):
        self.assertEqual(27, len(scope.included_codes()))
        self.assertEqual(scope.included_codes(), scope.core_odds_codes())
        self.assertTrue(scope.core_odds_codes().issubset(scope.odds_universe_codes()))

    def test_absolute_priority_is_main_five_plus_greece(self):
        self.assertEqual(
            {"E0", "D1", "I1", "SP1", "F1", "G1"},
            scope.absolute_priority_codes(),
        )

    def test_romania_code_is_normalized_in_all_scopes(self):
        self.assertTrue(scope.is_included("ROM"))
        self.assertTrue(scope.is_included("ROU"))
        self.assertTrue(scope.is_stats_included("ROM"))
        self.assertTrue(scope.is_stats_included("ROU"))
        self.assertTrue(scope.is_odds_included("ROM"))
        self.assertTrue(scope.is_odds_included("ROU"))

    def test_restored_leagues_are_odds_eligible_but_not_core_priority(self):
        for code in ("CZE", "SRB", "USA", "JPN", "BRA2", "AUT2", "SWE2"):
            self.assertTrue(scope.is_stats_included(code), code)
            self.assertTrue(scope.is_odds_included(code), code)
            self.assertFalse(scope.is_included(code), code)
            self.assertEqual(2, scope.priority_rank(code))

    def test_new_top_flights_are_stats_and_odds_eligible_but_not_core_priority(self):
        self.assertEqual(16, len(EXPANSION_CODES))
        for code in EXPANSION_CODES:
            self.assertTrue(scope.is_stats_included(code), code)
            self.assertTrue(scope.is_odds_included(code), code)
            self.assertFalse(scope.is_included(code), code)
            self.assertEqual(2, scope.priority_rank(code))

    def test_priority_tiers(self):
        self.assertEqual(0, scope.priority_rank("E0"))
        self.assertEqual(0, scope.priority_rank("G1"))
        self.assertEqual(1, scope.priority_rank("E1"))
        self.assertEqual(2, scope.priority_rank("CZE"))
        self.assertEqual(2, scope.priority_rank("AUS"))

    def test_core_registry_filter_keeps_only_core_priority_scope(self):
        payload = {
            "leagueCount": 5,
            "leagues": [
                {"leagueCode": "E0"},
                {"leagueCode": "G1"},
                {"leagueCode": "CZE"},
                {"leagueCode": "USA"},
                {"leagueCode": "AUS"},
            ],
        }
        filtered = scope.filter_registry_payload(payload)
        self.assertEqual(["E0", "G1"], [row["leagueCode"] for row in filtered["leagues"]])
        self.assertEqual(2, filtered["leagueCount"])
        self.assertEqual("core_odds", filtered["domesticScope"]["scopeType"])

    def test_stats_registry_filter_keeps_non_core_leagues(self):
        payload = {
            "leagueCount": 5,
            "leagues": [
                {"leagueCode": "E0"},
                {"leagueCode": "G1"},
                {"leagueCode": "CZE"},
                {"leagueCode": "USA"},
                {"leagueCode": "AUS"},
            ],
        }
        filtered = scope.filter_stats_registry_payload(payload)
        self.assertEqual(
            ["E0", "G1", "CZE", "USA", "AUS"],
            [row["leagueCode"] for row in filtered["leagues"]],
        )
        self.assertEqual(5, filtered["leagueCount"])
        self.assertEqual("stats_universe", filtered["domesticScope"]["scopeType"])

    def test_odds_registry_filter_keeps_non_core_leagues(self):
        payload = {
            "leagueCount": 5,
            "leagues": [
                {"leagueCode": "E0"},
                {"leagueCode": "G1"},
                {"leagueCode": "CZE"},
                {"leagueCode": "USA"},
                {"leagueCode": "AUS"},
            ],
        }
        filtered = scope.filter_odds_registry_payload(payload)
        self.assertEqual(
            ["E0", "G1", "CZE", "USA", "AUS"],
            [row["leagueCode"] for row in filtered["leagues"]],
        )
        self.assertEqual(5, filtered["leagueCount"])
        self.assertEqual("odds_universe", filtered["domesticScope"]["scopeType"])


if __name__ == "__main__":
    unittest.main()
