"""UEFA post-match refresh must not miss completed matchdays or waste quota."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from uefa_postmatch_refresh_due import due


class PostmatchRefreshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.at = datetime(2026, 10, 21, 6, tzinfo=timezone.utc)
        self.tables = self.root / "data/statmaker/league_tables"
        self.tables.mkdir(parents=True)
        self.stats_path = self.root / "data/statmaker/uefa_current_stats.json"
        self.fixture = {
            "fixture_id": 555,
            "date_utc": "2026-10-20T19:00:00+00:00",
            "status": "NS",
        }
        self.docs = {}
        for code in ["CL", "EL", "UECL"]:
            self.docs[code] = {
                "contract": "statmaker-league-tables-v1",
                "generatedAtUTC": "2026-10-20T12:14:45+00:00",
                "fixtures": [self.fixture.copy()] if code == "CL" else [],
                "standings": [], "scorers": {"verifiedFixtures": 0},
            }
        self.source = {"schemaVersion": 1, "generatedAt": "2026-10-20T05:26:04+00:00",
                       "competitions": [{"leagueCode": "CL", "fixtures": []}]}
        self.save()

    def save(self):
        for code, doc in self.docs.items():
            (self.tables / f"{code}.json").write_text(json.dumps(doc))
        self.stats_path.write_text(json.dumps(self.source))

    def test_yesterday_champions_league_due(self):
        self.assertTrue(due(self.root, "stats", self.at)[0])
        self.assertTrue(due(self.root, "tables", self.at)[0])

    def test_postmatch_done_skips_provider(self):
        self.docs["CL"]["fixtures"][0]["status"] = "FT"
        self.docs["CL"]["fixtures"][0]["home_goals"] = 2
        self.docs["CL"]["fixtures"][0]["away_goals"] = 1
        self.docs["CL"]["standings"] = [{"played": 1}, {"played": 1}]
        self.docs["CL"]["scorers"]["verifiedFixtures"] = 1
        self.source["competitions"][0]["fixtures"] = [{"fixtureId": 555, "status": "FT", "statsFetched": True, "normalizedStats": {"HS": 10, "AS": 8}}]
        self.save()
        self.assertFalse(due(self.root, "stats", self.at)[0])
        self.assertFalse(due(self.root, "tables", self.at)[0])

    def test_recent_retry_throttled(self):
        self.source["generatedAt"] = "2026-10-21T05:50:00+00:00"
        for doc in self.docs.values():
            doc["generatedAtUTC"] = "2026-10-21T05:20:00+00:00"
        self.save()
        self.assertFalse(due(self.root, "stats", self.at)[0])
        self.assertFalse(due(self.root, "tables", self.at)[0])

    def test_before_final_whistle_no_unneeded_calls(self):
        before = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
        self.assertFalse(due(self.root, "stats", before)[0])
        self.assertFalse(due(self.root, "tables", before)[0])

    def test_scorer_gap_retries(self):
        self.docs["CL"]["fixtures"][0]["status"] = "FT"
        self.docs["CL"]["standings"] = [{"played": 1}, {"played": 1}]
        self.docs["CL"]["scorers"]["verifiedFixtures"] = 0
        self.save()
        self.assertTrue(due(self.root, "tables", self.at)[0])

    def test_fresh_priority_final_triggers_uefa_stats_before_tables_catch_up(self):
        at = datetime(2026, 10, 20, 21, 5, tzinfo=timezone.utc)
        self.docs["CL"]["season"] = 2026
        self.docs["CL"]["apiFootballLeagueId"] = 2
        self.save()
        priority_dir = self.root / "data/statmaker/priority_refresh"
        priority_dir.mkdir(parents=True)
        (priority_dir / "CL.json").write_text(json.dumps({
            "contract": "statmaker-league-tables-v1",
            "leagueCode": "CL", "season": 2026,
            "apiFootballLeagueId": 2,
            "fixtures": [{
                "fixture_id": 555, "date_utc": "2026-10-20T19:00:00+00:00",
                "status": "FT", "home_goals": 2, "away_goals": 1
            }]
        }))
        self.assertTrue(due(self.root, "stats", at)[0])

    def test_no_provider_calls_on_nonmatch_day(self):
        self.fixture["date_utc"] = "2026-10-10T19:00:00+00:00"
        self.docs["CL"]["fixtures"][0] = self.fixture
        self.save()
        self.assertFalse(due(self.root, "tables", self.at)[0])


if __name__ == "__main__":
    unittest.main()
