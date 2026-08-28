import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rehydrate_domestic_betting_from_archive as target


class DomesticArchiveDroppedFixtureRehydrateTest(unittest.TestCase):
    def setUp(self):
        self.registry = [{"leagueCode": "TST"}]
        self.aliases = {
            "TST": {
                "provider home": "Canonical Home",
                "provider away": "Canonical Away",
            }
        }
        self.markets = [{
            "market": "1X2",
            "selection": "HOME",
            "bookmaker": "Bet365",
            "odds": 2.0,
            "exactBookmakerOdds": True,
        }]
        self.archive_match = {
            "id": "1001",
            "date": "2026-08-29",
            "kickoff": "2026-08-29T14:00:00Z",
            "providerHomeTeam": "Provider Home FC",
            "providerAwayTeam": "Provider Away FC",
            "teamMappingStatus": "unmatched",
            "providerMarkets": [{
                "bookmaker": "Bet365",
                "exactProviderPayload": True,
                "market": {"name": "ML", "odds": [{"home": "2.0", "draw": "3.0", "away": "4.0"}]},
            }],
        }

    def run_rebuild(self, feed, archive, aliases=None, markets=None):
        with (
            patch.object(target, "_historical_aliases", return_value=aliases if aliases is not None else self.aliases),
            patch.object(target, "_normalize_archived_markets", return_value=markets if markets is not None else self.markets),
            patch.object(target.pipeline, "today_utc", return_value=dt.date(2026, 8, 28)),
        ):
            return target.rebuild(feed, archive, self.registry)

    def test_missing_canonical_fixture_is_created_from_exact_archive(self):
        feed = {"leagues": [{"leagueCode": "TST", "matches": []}]}
        archive = {"leagues": [{"leagueCode": "TST", "matches": [dict(self.archive_match)]}]}

        report = self.run_rebuild(feed, archive)

        self.assertEqual(1, report["matchesRehydrated"])
        self.assertEqual(1, report["matchesCreatedFromArchive"])
        self.assertEqual(0, report["matchesRepairedExisting"])
        self.assertEqual(1, len(feed["leagues"][0]["matches"]))

        match = feed["leagues"][0]["matches"][0]
        self.assertEqual("1001", str(match["id"]))
        self.assertEqual("Canonical Home", match["homeTeam"])
        self.assertEqual("Canonical Away", match["awayTeam"])
        self.assertEqual("matched", match["teamMappingStatus"])
        self.assertTrue(match["usableForStats"])
        self.assertTrue(match["bettingRehydratedFromArchive"])
        self.assertEqual(self.markets, match["markets"])

    def test_existing_unhealthy_fixture_is_repaired_without_duplicate(self):
        feed = {"leagues": [{
            "leagueCode": "TST",
            "matches": [{
                "id": "1001",
                "date": "2026-08-29",
                "kickoff": "2026-08-29T14:00:00Z",
                "providerHomeTeam": "Provider Home FC",
                "providerAwayTeam": "Provider Away FC",
                "homeTeam": "Provider Home FC",
                "awayTeam": "Provider Away FC",
                "teamMappingStatus": "unmatched",
                "usableForStats": False,
                "markets": [],
            }],
        }]}
        archive = {"leagues": [{"leagueCode": "TST", "matches": [dict(self.archive_match)]}]}

        report = self.run_rebuild(feed, archive)

        self.assertEqual(1, report["matchesRehydrated"])
        self.assertEqual(0, report["matchesCreatedFromArchive"])
        self.assertEqual(1, report["matchesRepairedExisting"])
        self.assertEqual(1, len(feed["leagues"][0]["matches"]))
        self.assertTrue(feed["leagues"][0]["matches"][0]["usableForStats"])

    def test_unresolved_archive_fixture_remains_fail_closed(self):
        feed = {"leagues": [{"leagueCode": "TST", "matches": []}]}
        archive = {"leagues": [{"leagueCode": "TST", "matches": [dict(self.archive_match)]}]}

        report = self.run_rebuild(feed, archive, aliases={"TST": {}})

        self.assertEqual(0, report["matchesRehydrated"])
        self.assertEqual(0, report["matchesCreatedFromArchive"])
        self.assertEqual([], feed["leagues"][0]["matches"])
        self.assertEqual(1, len(report["unresolvedHistoricalTeams"]))


if __name__ == "__main__":
    unittest.main()
