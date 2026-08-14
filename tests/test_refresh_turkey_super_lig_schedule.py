import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refresh_turkey_super_lig_schedule as target


class TurkeySuperLigScheduleTest(unittest.TestCase):
    def fixture(self, fixture_id="9001"):
        return {
            "fixture": {
                "id": int(fixture_id),
                "date": "2026-08-15T18:00:00+00:00",
                "status": {"short": "NS"},
            },
            "teams": {
                "home": {"name": "Home SK", "logo": "home.png"},
                "away": {"name": "Away SK", "logo": "away.png"},
            },
        }

    def registry_row(self):
        return {
            "leagueCode": "T1",
            "apiFootballLeagueId": 203,
            "targetApiSeason": "2026",
            "targetAppSeason": "2026-2027",
            "providerLeagueSlug": None,
            "enabledForOdds": True,
            "enabledForBetting": True,
        }

    def test_registry_identity_is_locked(self):
        row = target.validate_registry({"leagues": [self.registry_row()]})
        self.assertEqual(203, row["apiFootballLeagueId"])
        self.assertEqual("2026", row["targetApiSeason"])
        self.assertIsNone(row["providerLeagueSlug"])

    def test_schedule_only_row_has_no_synthetic_markets(self):
        row = target.schedule_row(self.fixture())
        self.assertTrue(row["scheduleOnly"])
        self.assertTrue(row["scheduleVerified"])
        self.assertEqual("api-football", row["scheduleSource"])
        self.assertEqual([], row["markets"])

    def test_merge_preserves_existing_exact_markets_and_no_slug(self):
        feed = {
            "leagues": [{
                "leagueCode": "T1",
                "providerLeagueSlug": None,
                "matches": [{
                    "id": "9001",
                    "date": "2026-08-15",
                    "kickoff": "2026-08-15T18:00:00Z",
                    "homeTeam": "Home SK",
                    "awayTeam": "Away SK",
                    "teamMappingStatus": "matched",
                    "usableForStats": True,
                    "markets": [{"market": "1X2", "bookmaker": "Bet365", "odds": 2.0}],
                }],
            }],
        }
        merged, metrics = target.merge_schedule(
            feed, [self.fixture()], self.registry_row(), "2026-08-14T16:00:00Z"
        )
        league = merged["leagues"][0]
        self.assertIsNone(league["providerLeagueSlug"])
        self.assertEqual(1, len(league["matches"]))
        self.assertEqual(1, len(league["matches"][0]["markets"]))
        self.assertFalse(league["matches"][0]["scheduleOnly"])
        self.assertEqual(1, metrics["preservedBettable"])

    def test_registry_rejects_guessed_provider_slug(self):
        row = self.registry_row()
        row["providerLeagueSlug"] = "guessed-turkey-slug"
        with self.assertRaises(RuntimeError):
            target.validate_registry({"leagues": [row]})


if __name__ == "__main__":
    unittest.main()
