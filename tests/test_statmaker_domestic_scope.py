import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import statmaker_domestic_scope as scope


class StatMakerDomesticScopeTest(unittest.TestCase):
    def test_final_scope_contains_exactly_twenty_seven_leagues(self):
        self.assertEqual(27, len(scope.included_codes()))

    def test_absolute_priority_is_main_five_plus_greece(self):
        self.assertEqual(
            {"E0", "D1", "I1", "SP1", "F1", "G1"},
            scope.absolute_priority_codes(),
        )

    def test_romania_code_is_normalized(self):
        self.assertTrue(scope.is_included("ROM"))
        self.assertTrue(scope.is_included("ROU"))

    def test_rejected_leagues_are_outside_final_scope(self):
        for code in ("CZE", "SRB", "USA", "JPN", "BRA2", "AUT2", "SWE2"):
            self.assertFalse(scope.is_included(code), code)

    def test_registry_filter_removes_excluded_leagues(self):
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
        self.assertFalse(filtered["finalDomesticScope"]["excludedDomesticApiCallsAllowed"])


if __name__ == "__main__":
    unittest.main()
