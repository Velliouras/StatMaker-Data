import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refresh_domestic_odds_schedule_priority as target


class DomesticScheduleOnlyValidationTest(unittest.TestCase):
    def setUp(self):
        self.original = target.target.validate_feed
        target._install_schedule_only_validation()

    def tearDown(self):
        target.target.validate_feed = self.original

    def registry(self):
        return [{"leagueCode": "AUT"}]

    def test_verified_odds_api_schedule_only_unmapped_row_is_not_betting_validated(self):
        feed = {"leagues": [{
            "leagueCode": "AUT",
            "matches": [{
                "id": "72345460",
                "date": "2026-08-14",
                "providerHomeTeam": "LASK Linz",
                "providerAwayTeam": "SV Ried",
                "teamMappingStatus": "partial",
                "usableForStats": False,
                "scheduleOnly": True,
                "scheduleSource": "odds-api-io-events",
                "scheduleVerified": True,
                "markets": [],
            }],
        }]}
        result = target.target.validate_feed(feed, self.registry(), dt.date(2026, 8, 14))
        self.assertEqual(1, result["scheduleOnlyInRegistryMatchCount"])
        self.assertEqual(0, result["matchCount"])

    def test_schedule_row_with_market_still_requires_exact_mapping(self):
        feed = {"leagues": [{
            "leagueCode": "AUT",
            "matches": [{
                "id": "72345460",
                "date": "2026-08-14",
                "teamMappingStatus": "partial",
                "usableForStats": False,
                "scheduleOnly": True,
                "scheduleSource": "odds-api-io-events",
                "scheduleVerified": True,
                "markets": [{
                    "market": "1X2",
                    "bookmaker": "Bet365",
                    "odds": 2.0,
                    "exactBookmakerOdds": True,
                }],
            }],
        }]}
        with self.assertRaises(RuntimeError):
            target.target.validate_feed(feed, self.registry(), dt.date(2026, 8, 14))

    def test_unverified_zero_market_row_still_fails_closed(self):
        feed = {"leagues": [{
            "leagueCode": "AUT",
            "matches": [{
                "id": "72345460",
                "date": "2026-08-14",
                "teamMappingStatus": "partial",
                "usableForStats": False,
                "scheduleOnly": True,
                "scheduleSource": "odds-api-io-events",
                "scheduleVerified": False,
                "markets": [],
            }],
        }]}
        with self.assertRaises(RuntimeError):
            target.target.validate_feed(feed, self.registry(), dt.date(2026, 8, 14))


if __name__ == "__main__":
    unittest.main()
