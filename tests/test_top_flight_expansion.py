import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import domestic_odds_expansion as odds_expansion
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

    def test_verified_expansion_provider_slugs_are_pinned(self):
        rows = {row["leagueCode"]: row for row in expansion.expansion_leagues()}
        self.assertEqual("israel-premier-league", rows["ISR"]["providerLeagueSlug"])
        self.assertEqual("republic-of-korea-k-league-1", rows["KOR"]["providerLeagueSlug"])
        self.assertEqual("south-africa-premiership", rows["RSA"]["providerLeagueSlug"])

    def test_south_korea_verified_provider_alias_matches_republic_of_korea(self):
        class DummyOdds:
            @staticmethod
            def normalize_text(value):
                return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

            @staticmethod
            def provider_country_matches(config_league, provider_item):
                return False

        config = {
            "leagueCode": "KOR",
            "country": "South Korea",
            "competition": "K League 1",
            "providerLeagueSlug": "republic-of-korea-k-league-1",
            "searchTerms": ["south korea k league 1"],
        }
        provider = {
            "name": "Republic of Korea - K-League 1",
            "slug": "republic-of-korea-k-league-1",
        }
        matched = odds_expansion.strict_provider_league_match(
            DummyOdds,
            lambda _config, _providers: None,
            config,
            [provider],
        )
        self.assertIs(provider, matched)


if __name__ == "__main__":
    unittest.main()
