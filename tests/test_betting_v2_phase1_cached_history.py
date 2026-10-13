"""Offline Phase-1 cached history tests. No HTTP, secrets, actions or PROD writes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from phase1_cached_history import CachedHistoryBridge, team_key


def stamp(d: str):
    return datetime.fromisoformat(d.replace("Z", "+00:00")).astimezone(timezone.utc)


class CachedHistoryPhase1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = "data/api_football/fixture_stats/example/first/2026/fixture_stats.json"
        index = {"schema_version": 3, "leagues": [
            {"league_code": "E0", "league": "Premier League",
             "api_football_league_id": 39, "api_football_season": "2026",
             "cache_path": source},
        ]}
        self._write("data/statmaker/domestic_enriched/index.json", index)
        self.cached = {
            "provider": "api-football", "league_id": 39, "season": "2026",
            "schedule_fixtures": [
                {"fixture_id": 100, "date": "2026-02-15T15:00:00Z",
                 "home_team": "Example United FC", "away_team": "Sample Town",
                 "home_team_id": 11, "away_team_id": 22,
                 "source_league": {"id": 39, "season": 2026}, "status": "FT",
                 "home_goals": 2, "away_goals": 0},
                {"fixture_id": 200, "date": "2026-11-15T15:00:00Z",
                 "home_team": "Example United FC", "away_team": "Sample Town",
                 "home_team_id": 11, "away_team_id": 22,
                 "source_league": {"id": 39, "season": 2026}, "status": "NS"},
            ],
            "fixtures": [
                {"fixture_id": 100, "date": "2026-02-15T15:00:00Z",
                 "home_team": "Example United FC", "away_team": "Sample Town",
                 "source_league": {"id": 39, "season": 2026}, "status": "FT",
                 "home_goals": 2, "away_goals": 0,
                 "normalized_stats": {"HC": 8, "AC": 1, "HxG": 1.8, "AxG": 0.2}},
            ],
        }
        self.source = source
        self._write(self.source, self.cached)
        self.historical = [{
            "group": "E0|Premier League", "fixture_id": "100",
            "date": stamp("2026-02-15T15:00:00Z"),
            "home": "example united", "away": "sample town",
            "hg": 2, "ag": 0, "stats": {"HC": 8, "AC": 1},
        }]
        self.groups = {"E0|Premier League": {}}

    def _write(self, path: str, payload: dict) -> None:
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload), encoding="utf-8")

    def test_historical_team_ids_survive_name_variation(self):
        bridge = CachedHistoryBridge(self.root, self.historical, self.groups)
        self.assertEqual(bridge.history[0]["home"], team_key("39", "11"))
        result, reason, evidence = bridge.resolve({
            "leagueCode": "E0", "fixtureId": "external-bookmaker-37",
            "kickoff": "2026-11-15T15:00:00Z",
            "providerHomeTeam": "Example United FC",
            "providerAwayTeam": "Sample Town",
            "homeTeam": "Example United", "awayTeam": "Sample Town",
        })
        self.assertEqual(reason, "EXACT_API_TEAM_IDS")
        self.assertEqual(result, ("E0|Premier League", "api-team:39:11", "api-team:39:22"))
        self.assertEqual(evidence["apiFootballFixtureId"], "200")

    def test_wrong_provider_id_is_never_substituted(self):
        bridge = CachedHistoryBridge(self.root, self.historical, self.groups)
        result, reason, _ = bridge.resolve({
            "leagueCode": "E0", "fixtureId": "bookmaker-id",
            "kickoff": "2026-11-15T15:00:00Z",
            "providerHomeTeam": "Some Other Club",
            "providerAwayTeam": "Sample Town",
        })
        self.assertIsNone(result)
        self.assertEqual(reason, "NO_EXACT_CACHED_SCHEDULE_MATCH")

    def test_future_results_are_never_added_to_history(self):
        bridge = CachedHistoryBridge(self.root, self.historical, self.groups)
        self.assertEqual(len(bridge.history), 1)
        self.assertEqual(bridge.coverage()["cachedScheduledFixtures"], 1)

    def test_unindexed_old_cache_only_if_scored_and_league_id_known(self):
        old = "data/api_football/fixture_stats/example/first/2024/fixture_stats.json"
        self._write(old, {
            "provider": "api-football", "league_id": 39, "season": "2024",
            "schedule_fixtures": [{"fixture_id": 1,
                "home_team_id": 11, "away_team_id": 22,
                "source_league": {"id": 39, "season": 2024}}],
            "fixtures": [{"fixture_id": 1, "date": "2024-10-10T15:00:00Z",
                "status": "FT", "home_team": "Example United FC",
                "away_team": "Sample Town",
                "source_league": {"id": 39, "season": 2024},
                "home_goals": 1, "away_goals": 0,
                "normalized_stats": {"HC": 5, "AC": 2}}],
        })
        bridge = CachedHistoryBridge(self.root, self.historical, self.groups)
        self.assertEqual(bridge.stats["ADDED_SCORED_OLD_CACHE_ROWS"], 1)
        self.assertEqual(len(bridge.history), 2)
        self.assertEqual(bridge.history[1]["date"].year, 2024)

    def test_existing_history_without_ids_reuses_unique_real_provider_team_id(self):
        # Legacy enriched history lacks provider IDs, but the current cache
        # independently verifies the very same pair by league and team name.
        self.historical.append({
            "group": "E0|Premier League", "fixture_id": "historical-other",
            "date": stamp("2025-12-15T15:00:00Z"),
            "home": "Example United FC", "away": "Sample Town",
            "hg": 1, "ag": 0, "stats": {"HC": 5, "AC": 2},
        })
        bridge = CachedHistoryBridge(self.root, self.historical, self.groups)
        restored = next(x for x in bridge.history if x["fixture_id"] == "historical-other")
        self.assertEqual(restored["home"], "api-team:39:11")
        self.assertEqual(restored["away"], "api-team:39:22")
        self.assertEqual(restored["phase1TeamIdentityEvidence"],
                         "UNIQUE_VERIFIED_LEAGUE_TEAM_ALIAS")

    def test_conflicting_team_id_claim_keeps_unverified_row_separate(self):
        original = dict(self.cached["fixtures"][0])
        original["fixture_id"] = 101
        original["date"] = "2026-02-16T15:00:00Z"
        original["raw_statistics"] = [
            {"team": {"id": 999, "name": "Example United FC"}, "statistics": []},
            {"team": {"id": 22, "name": "Sample Town"}, "statistics": []}]
        self.cached["fixtures"].append(original)
        self.cached["schedule_fixtures"].append({
            "fixture_id":101,"date":"2026-02-16T15:00:00Z","status":"FT",
            "home_team":"Example United FC","away_team":"Sample Town",
            "home_team_id":999,"away_team_id":22,
            "source_league":{"id":39,"season":2026},
            "home_goals":1,"away_goals":0})
        self._write(self.source,self.cached)
        self.historical.append({
            "group":"E0|Premier League","fixture_id":"legacy-no-id",
            "date":stamp("2025-12-15T15:00:00Z"),
            "home":"Example United FC","away":"Sample Town",
            "hg":1,"ag":0,"stats":{}}
        )
        bridge=CachedHistoryBridge(self.root,self.historical,self.groups)
        entry=next(x for x in bridge.history if x["fixture_id"]=="legacy-no-id")
        self.assertEqual(entry["home"],"Example United FC")
        self.assertNotIn("phase1TeamIdentityEvidence",entry)

    def test_missing_score_does_not_create_fake_completed_history(self):
        old = "data/api_football/fixture_stats/example/first/2024/fixture_stats.json"
        self._write(old, {"provider": "api-football", "league_id": 39, "season": "2024",
            "schedule_fixtures": [], "fixtures": [{"fixture_id": 7,
                "date": "2024-11-10T15:00:00Z", "status": "FT",
                "home_team": "Another One", "away_team": "Other Club",
                "source_league": {"id": 39, "season": 2024},
                "normalized_stats": {"HC": 4, "AC": 3}}]})
        bridge = CachedHistoryBridge(self.root, self.historical, self.groups)
        self.assertEqual(len(bridge.history), 1)
        self.assertEqual(bridge.stats["OLD_CACHE_MISSING_SCORE"], 1)


if __name__ == "__main__":
    unittest.main()
