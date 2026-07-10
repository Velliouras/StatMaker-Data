import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import update_uefa_club_odds_api_io as uefa


class UefaClubOddsPipelineTest(unittest.TestCase):
    def test_champions_league_provider_match_rejects_europa(self):
        config = {
            "leagueCode": "CL",
            "searchTerms": ["uefa champions league", "champions league"],
        }
        provider = uefa.competition_provider_match(
            config,
            [
                {"name": "UEFA Europa League", "slug": "uefa-europa-league", "eventsCount": 20},
                {"name": "UEFA Champions League", "slug": "uefa-champions-league", "eventsCount": 10},
            ],
        )
        self.assertEqual("uefa-champions-league", provider["slug"])

    def test_europa_provider_match_rejects_conference(self):
        config = {
            "leagueCode": "EL",
            "searchTerms": ["uefa europa league", "europa league"],
        }
        provider = uefa.competition_provider_match(
            config,
            [
                {"name": "UEFA Europa Conference League", "slug": "uefa-europa-conference-league", "eventsCount": 50},
                {"name": "UEFA Europa League", "slug": "uefa-europa-league", "eventsCount": 10},
            ],
        )
        self.assertEqual("uefa-europa-league", provider["slug"])

    def test_canonical_alias_mapping(self):
        config = {
            "canonicalTeams": ["Paris", "Bodø/Glimt"],
            "aliases": {
                "Paris Saint-Germain": "Paris",
                "Bodo Glimt": "Bodø/Glimt",
            },
        }
        mapping = uefa.canonical_map(config)
        self.assertEqual("Paris", uefa.canonical_team("Paris Saint-Germain", mapping))
        self.assertEqual("Bodø/Glimt", uefa.canonical_team("Bodo/Glimt", mapping))
        self.assertIsNone(uefa.canonical_team("Unknown Qualifier", mapping))

    def test_merge_preserves_future_and_prunes_expired(self):
        today = dt.datetime.now(dt.timezone.utc).date()
        old_date = (today - dt.timedelta(days=1)).isoformat()
        future_date = (today + dt.timedelta(days=2)).isoformat()
        later_date = (today + dt.timedelta(days=3)).isoformat()
        merged = uefa.merge_matches(
            [
                {"id": "expired", "date": old_date},
                {"id": "keep", "date": future_date, "homeTeam": "A", "awayTeam": "B"},
            ],
            [
                {"id": "new", "date": later_date, "homeTeam": "C", "awayTeam": "D"},
            ],
        )
        self.assertEqual(["keep", "new"], [item["id"] for item in merged])


if __name__ == "__main__":
    unittest.main()
