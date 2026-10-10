"""Schedule scored-fixture/league scorers: verified player data only, zero HTTP."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_statmaker_schedule_scorers import build


class ScheduleScorersOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        directory = self.root / "data/statmaker/domestic_enriched"
        directory.mkdir(parents=True)
        self.events = self.root / "input_events"
        self.leaders = self.root / "input_leaders"
        self.events.mkdir()
        self.leaders.mkdir()
        (directory / "index.json").write_text(json.dumps({
            "schema_version": 3,
            "leagues": [{
                "league_code": "E0",
                "api_football_league_id": 39,
                "api_football_season": "2026",
                "app_season": "2026-2027",
                "stats_role": "current_target",
                "output_path": "data/statmaker/domestic_enriched/e0.json"
            }]
        }), encoding="utf-8")
        (directory / "e0.json").write_text(json.dumps({
            "competition": {"league_code": "E0"}, "matches": [
                {"status": "FT", "fixture_id": 500,
                 "home_team": "Arsenal", "away_team": "Brighton",
                 "home_goals": 2, "away_goals": 1},
                {"status": "NS", "fixture_id": 501,
                 "home_team": "Arsenal", "away_team": "Chelsea",
                 "home_goals": None, "away_goals": None}
            ]
        }), encoding="utf-8")

    def write_event(self, goals=3, fixture=500):
        goals_feed = []
        for index in range(goals):
            goals_feed.append({
                "type": "Goal", "detail": "Penalty" if index == 1 else "Normal Goal",
                "time": {"elapsed": 12 + index * 10, "extra": None},
                "team": {"name": "Arsenal" if index < 2 else "Brighton"},
                "player": {"name": "Player " + str(index)}
            })
        (self.events / "fixture500.json").write_text(json.dumps({
            "sourceRequest": "/fixtures/events?fixture=" + str(fixture),
            "fixtureId": fixture,
            "leagueCode": "E0", "retrievedAtUTC": "2026-10-10T12:00:00Z",
            "response": goals_feed
        }), encoding="utf-8")

    def write_leaders(self, league=39, season=2026):
        (self.leaders / "epl.json").write_text(json.dumps({
            "sourceRequest": "/players/topscorers?league=" + str(league) +
                "&season=" + str(season),
            "leagueCode": "E0", "apiFootballLeagueId": league,
            "season": season, "retrievedAtUTC": "2026-10-10T12:00:00Z",
            "response": [{
                "player": {"name": "Real Verified Name"},
                "statistics": [{
                    "league": {"id": league, "season": season},
                    "team": {"name": "Arsenal"},
                    "goals": {"total": 8, "assists": 2}
                }]
            }]
        }), encoding="utf-8")

    def test_empty_source_never_fabricates_scorers(self):
        doc = build(self.root, self.events, self.leaders)
        self.assertEqual(doc["goalEvents"], [])
        self.assertEqual(doc["leagueTopScorers"], [])
        self.assertEqual(doc["apiCallsDuringGeneration"], 0)

    def test_goal_reconciliation_and_penalty_details(self):
        self.write_event()
        doc = build(self.root, self.events, self.leaders)
        self.assertEqual(len(doc["goalEvents"]), 3)
        self.assertEqual([x["minute"] for x in doc["goalEvents"]], [12, 22, 32])
        self.assertTrue(doc["goalEvents"][1]["penalty"])
        self.assertEqual(doc["goalEvents"][2]["team"], "Brighton")

    def test_incomplete_events_not_misrepresented_as_all_scorers(self):
        self.write_event(goals=2)
        doc = build(self.root, self.events, self.leaders)
        self.assertEqual(doc["goalEvents"], [])
        self.assertEqual(doc["rejections"]["score_event_count_mismatch"], 1)

    def test_wrong_fixture_identity_rejected(self):
        self.write_event(fixture=501)
        doc = build(self.root, self.events, self.leaders)
        self.assertEqual(doc["goalEvents"], [])
        self.assertEqual(doc["rejections"]["unknown_or_repeated_fixture"], 1)

    def test_six_league_top_scorers_are_strictly_provenanced(self):
        self.write_leaders()
        doc = build(self.root, self.events, self.leaders)
        self.assertEqual(doc["leagueTopScorers"][0]["leagueCode"], "E0")
        self.assertEqual(doc["leagueTopScorers"][0]["players"][0]["goals"], 8)
        self.assertEqual(doc["leagueTopScorers"][0]["players"][0]["assists"], 2)
        self.assertTrue(doc["leagueTopScorers"][0]["verified"])

    def test_wrong_league_season_never_publishes_leaders(self):
        self.write_leaders(season=2025)
        doc = build(self.root, self.events, self.leaders)
        self.assertEqual(doc["leagueTopScorers"], [])
        self.assertEqual(doc["rejections"]["invalid_topscorer_provenance"], 1)


if __name__ == "__main__":
    unittest.main()
