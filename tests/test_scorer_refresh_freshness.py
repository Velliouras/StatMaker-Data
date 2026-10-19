"""Regression tests for scorer cache freshness and recent-match prioritization."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fetch_schedule_scorers_capped import plan, recent_leader_cache
from refresh_domestic_match_context import fixture_priority, refresh_window, already_refreshed


NOW = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)


class ScorerRefreshRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        folder = self.root / "data/statmaker/domestic_enriched"
        folder.mkdir(parents=True)
        (folder / "index.json").write_text(json.dumps({
            "schema_version": 3,
            "leagues": [{
                "league_code": "E0", "lifecycle": "active",
                "stats_role": "current_target",
                "api_football_season": 2026,
                "api_football_league_id": 39
            }]
        }), encoding="utf-8")
        self.cache = self.root / "data/api_football/schedule_scorers/leaders/E0_2026.json"
        self.cache.parent.mkdir(parents=True)

    def write_cache(self, when, source="/players/topscorers?league=39&season=2026"):
        self.cache.write_text(json.dumps({
            "sourceRequest": source,
            "apiFootballLeagueId": 39, "season": 2026,
            "retrievedAtUTC": when,
            "response": [{"player": {"name": "Verified"}}]
        }), encoding="utf-8")

    def test_stale_provenance_does_not_become_fresh_with_checkout_mtime(self):
        self.write_cache("2026-10-11T14:57:21+00:00")
        os.utime(self.cache, None)  # Fresh filesystem mtime, old provider data.
        self.assertFalse(recent_leader_cache(self.cache, 39, 2026, NOW))

    def test_fresh_provenance_survives_old_file_mtime(self):
        self.write_cache("2026-10-19T07:00:00+00:00")
        os.utime(self.cache, (1_600_000_000, 1_600_000_000))
        self.assertTrue(recent_leader_cache(self.cache, 39, 2026, NOW))

    def test_bad_identity_future_and_missing_timestamp_must_refresh(self):
        self.write_cache(NOW.isoformat(), source="/players/topscorers?league=40&season=2026")
        self.assertFalse(recent_leader_cache(self.cache, 39, 2026, NOW))
        self.write_cache((NOW+timedelta(hours=1)).isoformat())
        self.assertFalse(recent_leader_cache(self.cache, 39, 2026, NOW))
        self.write_cache("not-a-date")
        self.assertFalse(recent_leader_cache(self.cache, 39, 2026, NOW))

    def test_missing_leader_cache_is_scheduled(self):
        entries = plan(self.root, "leaders", 14)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], "players/topscorers")
        self.assertEqual(entries[0][1], {"league": 39, "season": 2026})

    def test_recent_matches_across_leagues_outrank_old_priority_league(self):
        today = date(2026, 10, 19)
        old_greek = {"leagueCode": "G1", "date": "2026-10-07T18:00:00+00:00", "fixtureId": 1}
        recent_english = {"leagueCode": "E0", "date": "2026-10-18T18:00:00+00:00", "fixtureId": 2}
        self.assertLess(fixture_priority(recent_english, today),
                        fixture_priority(old_greek, today))

    def test_night_window_spans_midnight(self):
        before=datetime(2026, 10, 19, 23, 35, tzinfo=timezone.utc)
        after=datetime(2026, 10, 20, 1, 20, tzinfo=timezone.utc)
        self.assertEqual(refresh_window(before)[0], "postmatch-2026-10-19")
        self.assertEqual(refresh_window(after)[0], "postmatch-2026-10-19")

    def test_new_morning_window_needs_new_refresh(self):
        now=datetime(2026, 10, 20, 7, 10, tzinfo=timezone.utc)
        self.assertEqual(refresh_window(now)[0], "morning-2026-10-20")
        previous={"contract":"statmaker-domestic-match-context-v1",
                  "generatedAtUTC":"2026-10-20T01:20:00+00:00",
                  "refreshWindowKey":"postmatch-2026-10-19",
                  "apiCallsDuringGeneration":32}
        self.assertFalse(already_refreshed(previous, now))

    def test_duplicate_provider_spending_is_blocked(self):
        now=datetime(2026, 10, 20, 8, 20, tzinfo=timezone.utc)
        doc={"contract":"statmaker-domestic-match-context-v1",
             "generatedAtUTC":"2026-10-20T07:11:00+00:00",
             "refreshWindowKey":"morning-2026-10-20",
             "apiCallsDuringGeneration":32}
        self.assertTrue(already_refreshed(doc, now))

    def test_legacy_good_refresh_prevents_duplicate_run(self):
        now=datetime(2026, 10, 19, 8, 20, tzinfo=timezone.utc)
        doc={"contract":"statmaker-domestic-match-context-v1",
             "generatedAtUTC":"2026-10-19T06:51:22+00:00",
             "apiCallsDuringGeneration":32}
        self.assertTrue(already_refreshed(doc, now))
        doc["apiCallsDuringGeneration"]=0
        self.assertFalse(already_refreshed(doc, now))

    def test_recent_priority_leagues_outrank_other_recent_leagues(self):
        today = date(2026, 10, 19)
        greek = {"leagueCode": "G1", "date": "2026-10-18T18:00:00+00:00", "fixtureId": 1}
        other = {"leagueCode": "JPN", "date": "2026-10-19T01:00:00+00:00", "fixtureId": 2}
        self.assertLess(fixture_priority(greek, today),
                        fixture_priority(other, today))


if __name__ == "__main__":
    unittest.main()
