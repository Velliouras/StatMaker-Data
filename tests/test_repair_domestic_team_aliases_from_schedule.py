import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import repair_domestic_team_aliases_from_schedule as target


class DomesticVerifiedScheduleAliasRepairTest(unittest.TestCase):
    def setUp(self):
        self.archive_match = {
            "id": "42",
            "date": "2026-08-29",
            "kickoff": "2026-08-29T14:00:00Z",
            "providerHomeTeam": "Bayern Munich",
            "providerAwayTeam": "VfB Stuttgart",
        }

    def test_unique_kickoff_fixture_resolves_without_name_fuzzy_matching(self):
        unresolved = {
            "leagueCode": "D1",
            "matchId": "42",
            "homeResolved": None,
            "awayResolved": "VfB Stuttgart",
        }
        fixtures = [
            {
                "fixtureId": 9001,
                "date": "2026-08-29",
                "kickoff": dt.datetime(2026, 8, 29, 14, 0, tzinfo=dt.timezone.utc),
                "homeTeam": "Bayern München",
                "awayTeam": "VfB Stuttgart",
            },
            {
                "fixtureId": 9002,
                "date": "2026-08-29",
                "kickoff": dt.datetime(2026, 8, 29, 16, 30, tzinfo=dt.timezone.utc),
                "homeTeam": "Other Home",
                "awayTeam": "Other Away",
            },
        ]

        resolved = target.unique_fixture_identity(
            self.archive_match,
            unresolved,
            fixtures,
        )

        self.assertIsNotNone(resolved)
        self.assertEqual("Bayern München", resolved["homeTeam"])
        self.assertEqual(9001, resolved["fixtureId"])

    def test_two_unresolved_sides_require_unique_close_kickoff(self):
        unresolved = {
            "leagueCode": "D1",
            "matchId": "42",
            "homeResolved": None,
            "awayResolved": None,
        }
        fixtures = [
            {
                "fixtureId": 1,
                "date": "2026-08-29",
                "kickoff": dt.datetime(2026, 8, 29, 13, 30, tzinfo=dt.timezone.utc),
                "homeTeam": "A",
                "awayTeam": "B",
            },
            {
                "fixtureId": 2,
                "date": "2026-08-29",
                "kickoff": dt.datetime(2026, 8, 29, 14, 30, tzinfo=dt.timezone.utc),
                "homeTeam": "C",
                "awayTeam": "D",
            },
        ]

        self.assertIsNone(
            target.unique_fixture_identity(self.archive_match, unresolved, fixtures)
        )

    def test_unique_roster_anchor_is_only_used_when_league_local_token_is_unique(self):
        rosters = {
            "D1": ["1. FC Köln", "1899 Hoffenheim", "Bayern München"],
            "AUT2": ["FC BW Linz", "WSPG Wels", "Schwarz-Weiß Bregenz"],
        }

        self.assertEqual(
            "1899 Hoffenheim",
            target.unique_roster_anchor("TSG Hoffenheim", "D1", rosters),
        )
        self.assertEqual(
            "FC BW Linz",
            target.unique_roster_anchor("Blau-Weiss Linz", "AUT2", rosters),
        )
        self.assertIsNone(
            target.unique_roster_anchor("FC United", "D1", rosters),
        )

    def test_exact_normalized_anchor_outweighs_weak_city_token_anchor(self):
        rosters = {
            "AUT2": [
                "Austria Vienna (Am)",
                "Rapid Wien II",
                "Schwarz-Weiß Bregenz",
            ],
        }
        home = target.unique_roster_anchor(
            "Young Violets Wien", "AUT2", rosters
        )
        away = target.unique_roster_anchor(
            "Schwarz-Weiss Bregenz", "AUT2", rosters
        )

        self.assertEqual("Rapid Wien II", home)
        self.assertEqual("Schwarz-Weiß Bregenz", away)
        self.assertEqual(
            1,
            target.roster_anchor_strength("Young Violets Wien", home),
        )
        self.assertEqual(
            2,
            target.roster_anchor_strength("Schwarz-Weiss Bregenz", away),
        )

    def test_verified_alias_never_overwrites_conflicting_owner(self):
        payload = {
            "version": 1,
            "normalizationRules": {},
            "aliases": {
                "D1": {
                    "Existing Canonical": ["Bayern Munich"],
                }
            },
        }

        added, conflict = target.add_verified_alias(
            payload,
            "D1",
            "Bayern München",
            "Bayern Munich",
        )

        self.assertFalse(added)
        self.assertEqual("conflict:Existing Canonical", conflict)
        self.assertNotIn("Bayern München", payload["aliases"]["D1"])


if __name__ == "__main__":
    unittest.main()
