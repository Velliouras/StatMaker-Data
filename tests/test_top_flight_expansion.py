import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import statmaker_top_flight_expansion as expansion


EXPECTED_CODES = {
    "CRO", "CYP", "ISR", "UKR", "AUS", "KOR", "SAU", "UAE",
    "COL", "CHL", "URU", "PER", "ECU", "EGY", "MAR", "RSA",
}


class TopFlightExpansionTest(unittest.TestCase):
    def test_contract_has_exactly_sixteen_unique_first_divisions(self):
        expansion.validate_contract()
        self.assertEqual(EXPECTED_CODES, expansion.expansion_codes())

    def test_domestic_merge_preserves_existing_rows_and_adds_group(self):
        source = {
            "version": 6,
            "groups": {"all_initial": ["E0"], "all_blue_yellow": ["E0"]},
            "leagues": [{"leagueCode": "E0", "country": "England"}],
        }
        merged = expansion.merge_domestic_config(source)
        codes = {row["leagueCode"] for row in merged["leagues"]}
        self.assertEqual({"E0"} | EXPECTED_CODES, codes)
        self.assertEqual(16, len(merged["groups"][expansion.PRIORITY_GROUP]))
        self.assertTrue(EXPECTED_CODES.issubset(set(merged["groups"]["all_initial"])))
        self.assertTrue(EXPECTED_CODES.issubset(set(merged["groups"]["all_blue_yellow"])))
        self.assertEqual(7, merged["version"])

    def test_enrichment_merge_adds_api_ids_without_changing_existing(self):
        source = {
            "version": 4,
            "leagues": [
                {
                    "leagueCode": "E0",
                    "country": "England",
                    "api_football_league_id": 39,
                }
            ],
        }
        merged = expansion.merge_enrichment_config(source)
        rows = {row["leagueCode"]: row for row in merged["leagues"]}
        self.assertEqual(39, rows["E0"]["api_football_league_id"])
        self.assertEqual(188, rows["AUS"]["api_football_league_id"])
        self.assertEqual(210, rows["CRO"]["api_football_league_id"])
        self.assertEqual(expansion.PRIORITY_GROUP, rows["AUS"]["priority_group"])
        self.assertEqual(5, merged["version"])


if __name__ == "__main__":
    unittest.main()
