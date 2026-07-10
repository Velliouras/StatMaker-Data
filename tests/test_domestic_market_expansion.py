import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import odds_api_io_market_audit as audit
import update_domestic_odds_api_io as odds


class DomesticMarketExpansionTest(unittest.TestCase):
    def test_double_chance_is_supported(self):
        result = audit.classify_provider_market("Double Chance")
        self.assertEqual("DOUBLE_CHANCE", result["family"])
        self.assertEqual("supported", result["status"])

    def test_unibet_double_chance_shape(self):
        debug = {}
        rows = odds.normalize_market(
            {
                "name": "Double Chance",
                "odds": [{"1X": "1.24", "12": "1.23", "X2": "1.80"}],
            },
            "Unibet",
            "VPS",
            "SJK",
            debug,
        )
        by_selection = {row["selection"]: row for row in rows}
        self.assertEqual({"1X", "12", "X2"}, set(by_selection))
        self.assertEqual(1.24, by_selection["1X"]["odds"])
        self.assertEqual(1.23, by_selection["12"]["odds"])
        self.assertEqual(1.80, by_selection["X2"]["odds"])
        self.assertTrue(all(row["exactBookmakerOdds"] for row in rows))

    def test_bet365_label_double_chance_shape(self):
        debug = {}
        rows = odds.normalize_market(
            {
                "name": "Double Chance",
                "odds": [
                    {"label": "VPS Vaasa or Draw", "under": "1.363"},
                    {"label": "Draw or SJK", "under": "1.727"},
                    {"label": "VPS Vaasa or SJK", "under": "1.285"},
                ],
            },
            "Bet365",
            "VPS",
            "SJK",
            debug,
        )
        self.assertEqual(
            {"1X": 1.363, "X2": 1.727, "12": 1.285},
            {row["selection"]: row["odds"] for row in rows},
        )

    def test_club_prefix_simplification(self):
        self.assertEqual("necaxa", odds.simplified_team_name("Club Necaxa"))
        self.assertEqual("fcsb", odds.simplified_team_name("Fotbal Club FCSB"))
        self.assertEqual("toluca", odds.simplified_team_name("Deportivo Toluca FC"))

    def test_exact_alias_is_preferred(self):
        debug = {}
        mapped, canonical = odds.canonical_team_info(
            "Club Necaxa",
            "MEX",
            {"MEX": {"necaxa": "Necaxa"}},
            debug,
        )
        self.assertEqual("Necaxa", mapped)
        self.assertEqual("Necaxa", canonical)
        self.assertFalse(debug.get("unmatchedTeams"))


if __name__ == "__main__":
    unittest.main()
