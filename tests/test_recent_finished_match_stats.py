"""Offline tests for low-latency, quota-capped postmatch statistics."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts/api_budget_runtime"))
import refresh_recent_finished_match_stats as live

NOW = datetime(2026, 10, 31, 17, 40, tzinfo=timezone.utc)
CACHE = "data/api_football/fixture_stats/england/premier-league/2026/fixture_stats.json"


def dump(root, path, data):
    path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class LivePostmatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        dump(self.root, "data/statmaker/domestic_enriched/index.json", {"leagues": [{
            "league_code": "E0", "api_football_season": "2026",
            "api_football_league_id": 39, "cache_path": CACHE,
            "country": "England", "league": "Premier League",
        }]})
        dump(self.root, CACHE, {"league_id": 39, "season": "2026",
                                "country": "England", "league_name": "Premier League",
                                "fixtures": [], "schedule_fixtures": [], "roster": []})
        self.fixture = {
            "fixture_id": 123456, "date_utc": (NOW - timedelta(hours=2)).isoformat(),
            "status": "FT", "home_goals": 2, "away_goals": 1,
            "home_team": "Home", "away_team": "Away",
            "home_team_id": 100, "away_team_id": 200,
            "round": "Regular Season - 10",
        }
        self.save_priority()

    def save_priority(self):
        dump(self.root, "data/statmaker/priority_refresh/E0.json", {
            "leagueCode": "E0", "season": 2026, "apiFootballLeagueId": 39,
            "fixtures": [self.fixture],
        })

    def provider(self, *args):
        key, endpoint, params, counter, maximum = args
        self.assertEqual(endpoint, "fixtures/statistics")
        self.assertEqual(params, {"fixture": 123456})
        self.assertLess(counter["count"], maximum)
        counter["count"] += 1
        return {"response": [
            {"team": {"id": 100}, "statistics": [
                {"type": "Total Shots", "value": 11},
                {"type": "Corner Kicks", "value": 5},
                {"type": "Shots off Goal", "value": 4}]},
            {"team": {"id": 200}, "statistics": [
                {"type": "Total Shots", "value": 9},
                {"type": "Corner Kicks", "value": 3}]},
        ]}

    def test_final_fetch_once_and_persist_to_existing_canonical_cache(self):
        selected = live.candidates(self.root, NOW)
        self.assertEqual(len(selected), 1)
        outcome = live.refresh(root=self.root, now=NOW, maximum=4, fetch=self.provider)
        self.assertEqual(outcome["updated"], 1)
        self.assertEqual(outcome["requests"], 1)
        cached = live.read(self.root / CACHE)
        self.assertEqual(len(cached["fixtures"]), 1)
        row = cached["fixtures"][0]
        self.assertEqual(row["normalized_stats"]["HS"], 11)
        self.assertEqual(row["normalized_stats"]["AS"], 9)
        self.assertEqual(row["normalized_stats"]["HC"], 5)
        self.assertEqual(row["normalized_stats"]["HShotsOffGoal"], 4)
        self.assertEqual(row["fixture_id"], 123456)
        self.assertEqual(row["source_league"]["id"], 39)
        self.assertEqual(live.candidates(self.root, NOW + timedelta(minutes=15)), [])
        again = live.refresh(root=self.root, now=NOW + timedelta(minutes=15),
                             maximum=4, fetch=self.provider)
        self.assertEqual(again["requests"], 0)

    def test_no_prematch_or_live_or_outside_window_calls(self):
        for status in ("NS", "1H", "HT"):
            self.fixture["status"] = status
            self.save_priority()
            self.assertEqual(live.candidates(self.root, NOW), [])
        self.fixture["status"] = "FT"
        self.fixture["date_utc"] = (NOW - timedelta(hours=11)).isoformat()
        self.save_priority()
        self.assertEqual(live.candidates(self.root, NOW), [])

    def test_empty_provider_response_has_cooldown_then_retries(self):
        calls = []
        def empty(key, endpoint, params, counter, maximum):
            counter["count"] += 1
            calls.append(params["fixture"])
            return {"response": []}
        first = live.refresh(root=self.root, now=NOW, maximum=4, fetch=empty)
        self.assertEqual((first["updated"], first["requests"]), (0, 1))
        self.assertTrue(first["stateChanged"])
        second = live.refresh(root=self.root, now=NOW + timedelta(minutes=5),
                              maximum=4, fetch=empty)
        self.assertEqual(second["requests"], 0)
        third = live.refresh(root=self.root, now=NOW + timedelta(minutes=13),
                             maximum=4, fetch=self.provider)
        self.assertEqual((third["updated"], third["requests"]), (1, 1))
        self.assertEqual(calls, [123456])

    def test_cross_season_cache_is_rejected_without_network(self):
        dump(self.root, CACHE, {"league_id": 39, "season": "2025", "fixtures": []})
        self.assertEqual(live.candidates(self.root, NOW), [])

    def test_every_league_uses_shared_five_minute_final_detection(self):
        policy = json.loads((ROOT / "config/api_football_refresh_policy.json").read_text())
        self.assertEqual(
            {key: item["resultsMinutes"] for key, item in policy["tiers"].items()},
            {"primary": 5, "europe": 5, "background": 5},
        )
        self.assertEqual((policy["totalStop"], policy["reserve"]), (6000, 1500))

    def test_workflow_has_guard_and_refreshes_app_ready_only_after_stats(self):
        workflow = (ROOT / ".github/workflows/recent-postmatch-stats.yml").read_text()
        self.assertIn('STATMAKER_API_BUDGET_ENFORCE: "true"', workflow)
        self.assertIn("scripts/refresh_recent_finished_match_stats.py --max-requests 24", workflow)
        self.assertIn("workflow_run:", workflow)
        self.assertIn("Priority Results and Official Standings", workflow)
        self.assertIn("if: steps.publish.outputs.updated == 'true'", workflow)
        self.assertIn("app-ready-stats-hot-publish.yml/dispatches", workflow)
        hot = (ROOT / ".github/workflows/app-ready-stats-hot-publish.yml").read_text()
        self.assertIn("  workflow_dispatch:", hot.split("permissions:", 1)[0])


if __name__ == "__main__":
    unittest.main()
