import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import domestic_live_july_pipeline as pipeline
import refresh_domestic_live_july_odds as target


class RecanonicalizeStaleMappingsTest(unittest.TestCase):
    def aliases(self):
        registry = pipeline.load_json(pipeline.REGISTRY_PATH, {}).get("leagues", [])
        return pipeline.generated_aliases(registry)

    def test_repairs_verified_arg_stale_mapping(self):
        feed = {"leagues": [{
            "leagueCode": "ARG",
            "matches": [{
                "id": "73265188",
                "providerHomeTeam": "Racing Club Avellaneda",
                "providerAwayTeam": "CA Banfield",
                "homeTeam": "Racing Club Avellaneda",
                "awayTeam": "CA Banfield",
                "canonicalHomeTeam": None,
                "canonicalAwayTeam": None,
                "teamMappingStatus": "unmatched",
                "usableForStats": False,
                "markets": [],
            }],
        }]}
        repaired = target.recanonicalize_stale_team_mappings(feed, self.aliases())
        match = feed["leagues"][0]["matches"][0]
        self.assertEqual(1, repaired)
        self.assertEqual("Racing Club", match["homeTeam"])
        self.assertEqual("Banfield", match["awayTeam"])
        self.assertEqual("matched", match["teamMappingStatus"])
        self.assertTrue(match["usableForStats"])

    def test_fails_closed_when_alias_is_not_verified(self):
        feed = {"leagues": [{
            "leagueCode": "ARG",
            "matches": [{
                "providerHomeTeam": "Unknown Argentina Club",
                "providerAwayTeam": "CA Banfield",
                "teamMappingStatus": "partial",
                "usableForStats": False,
                "markets": [],
            }],
        }]}
        repaired = target.recanonicalize_stale_team_mappings(feed, self.aliases())
        match = feed["leagues"][0]["matches"][0]
        self.assertEqual(0, repaired)
        self.assertEqual("partial", match["teamMappingStatus"])
        self.assertFalse(match["usableForStats"])

    def test_does_not_touch_verified_api_schedule_only_rows(self):
        feed = {"leagues": [{
            "leagueCode": "T1",
            "matches": [{
                "providerHomeTeam": "Galatasaray",
                "providerAwayTeam": "Fenerbahce",
                "teamMappingStatus": "schedule_only_api_football",
                "usableForStats": False,
                "scheduleOnly": True,
                "scheduleVerified": True,
                "markets": [],
            }],
        }]}
        repaired = target.recanonicalize_stale_team_mappings(feed, self.aliases())
        match = feed["leagues"][0]["matches"][0]
        self.assertEqual(0, repaired)
        self.assertEqual("schedule_only_api_football", match["teamMappingStatus"])
        self.assertFalse(match["usableForStats"])


if __name__ == "__main__":
    unittest.main()
