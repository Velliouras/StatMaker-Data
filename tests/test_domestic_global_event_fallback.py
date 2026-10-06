import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refresh_domestic_odds_schedule_priority as schedule_priority


class DomesticGlobalEventFallbackTest(unittest.TestCase):
    def setUp(self):
        self.original = schedule_priority.target.odds_fetch.fetch_events_for_league

    def tearDown(self):
        schedule_priority.target.odds_fetch.fetch_events_for_league = self.original

    def test_empty_league_response_uses_same_slug_global_events(self):
        schedule_priority.target.odds_fetch.fetch_events_for_league = (
            lambda api_key, slug, horizon_days, debug: []
        )
        global_events = [
            {
                "id": 101,
                "date": "2026-10-09T17:30:00Z",
                "league": {"slug": "argentina-liga-profesional"},
            },
            {
                "id": 202,
                "date": "2026-10-09T17:00:00Z",
                "league": {"slug": "denmark-superliga"},
            },
        ]
        schedule_priority._install_near_term_event_horizon(7, global_events)

        debug = {}
        rows = schedule_priority.target.odds_fetch.fetch_events_for_league(
            "key", "argentina-liga-profesional", 7, debug
        )

        self.assertEqual([101], [row["id"] for row in rows])
        self.assertEqual(
            "verified-global-events-only",
            debug["globalEventFallbacks"][0]["policy"],
        )

    def test_nonempty_league_response_remains_authoritative(self):
        local_event = {
            "id": 303,
            "date": "2026-10-10T12:00:00Z",
            "league": {"slug": "denmark-superliga"},
        }
        schedule_priority.target.odds_fetch.fetch_events_for_league = (
            lambda api_key, slug, horizon_days, debug: [local_event]
        )
        global_events = [
            {
                "id": 404,
                "date": "2026-10-10T12:00:00Z",
                "league": {"slug": "denmark-superliga"},
            }
        ]
        schedule_priority._install_near_term_event_horizon(7, global_events)

        debug = {}
        rows = schedule_priority.target.odds_fetch.fetch_events_for_league(
            "key", "denmark-superliga", 7, debug
        )

        self.assertEqual([303], [row["id"] for row in rows])
        self.assertNotIn("globalEventFallbacks", debug)


if __name__ == "__main__":
    unittest.main()
