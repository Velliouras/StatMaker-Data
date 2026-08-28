import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import repair_domestic_api_football_league_ids as target


class DomesticLeagueIdentityRepairTest(unittest.TestCase):
    def test_unique_exact_country_season_competition_match_is_selected(self):
        meta = {
            "country": "Finland",
            "competition": "Ykkösliiga",
            "display_name": "Ykkösliiga",
        }
        items = [
            {
                "league": {"id": 900, "name": "Ykkönen"},
                "country": {"name": "Finland"},
            },
            {
                "league": {"id": 901, "name": "Ykkösliiga"},
                "country": {"name": "Finland"},
            },
        ]

        candidate = target.exact_catalog_match(meta, items)

        self.assertEqual(901, candidate["id"])
        self.assertEqual("Ykkösliiga", candidate["name"])

    def test_ambiguous_exact_match_fails_closed(self):
        meta = {
            "country": "Finland",
            "competition": "Ykkösliiga",
            "display_name": "Ykkösliiga",
        }
        items = [
            {"league": {"id": 901, "name": "Ykkösliiga"}, "country": {"name": "Finland"}},
            {"league": {"id": 902, "name": "Ykkösliiga"}, "country": {"name": "Finland"}},
        ]
        self.assertIsNone(target.exact_catalog_match(meta, items))

    def test_update_ids_only_changes_requested_league(self):
        payload = {
            "leagues": [
                {"leagueCode": "FIN2", "apiFootballLeagueId": 245, "api_football_league_id": 245},
                {"leagueCode": "FIN", "apiFootballLeagueId": 244},
            ]
        }

        changed = target.update_ids(payload, "FIN2", 901)

        self.assertEqual(2, changed)
        self.assertEqual(901, payload["leagues"][0]["apiFootballLeagueId"])
        self.assertEqual(901, payload["leagues"][0]["api_football_league_id"])
        self.assertEqual(244, payload["leagues"][1]["apiFootballLeagueId"])

    def test_remove_roster_code_removes_stale_entry(self):
        payload = {
            "leagueCount": 2,
            "leagues": [
                {"leagueCode": "FIN2", "appSeason": "2026", "teams": ["Wrong A", "Wrong B"]},
                {"leagueCode": "FIN", "appSeason": "2026", "teams": ["HJK", "Inter Turku"]},
            ],
        }

        removed = target.remove_roster_code(payload, "FIN2")

        self.assertEqual(1, removed)
        self.assertEqual(1, payload["leagueCount"])
        self.assertEqual(["FIN"], [row["leagueCode"] for row in payload["leagues"]])


if __name__ == "__main__":
    unittest.main()
