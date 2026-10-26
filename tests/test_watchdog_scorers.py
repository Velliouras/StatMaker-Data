"""Exercise scorer freshness, verified caches and the external watchdog route."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import refresh_domestic_match_context as context
import fetch_schedule_scorers_capped as leaders
import build_league_tables_program as programme
from build_statmaker_schedule_scorers import canonical_index
from scorer_refresh_sources import priority_completed
from uefa_postmatch_refresh_due import due

NOW = datetime(2026, 10, 26, 8, tzinfo=timezone.utc)


class WatchdogScorersTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache = self.root / "data/api_football/schedule_scorers"
        self.entry = {"league_code": "E0", "lifecycle": "active", "stats_role": "current_target",
                      "api_football_league_id": 39, "api_football_season": 2026,
                      "output_path": "data/statmaker/domestic_enriched/e0.json"}
        self.final = {"fixture_id": 500, "status": "FT", "date_utc": (NOW-timedelta(hours=3)).isoformat(),
                      "home_team": "Arsenal", "away_team": "Brighton", "home_team_id": 42,
                      "away_team_id": 51, "home_goals": 1, "away_goals": 1, "venue": "Actual venue"}
        self.doc = {"contract": programme.CONTRACT, "leagueCode": "E0", "season": 2026,
                    "apiFootballLeagueId": 39, "fixtures": [self.final]}
        self.fixture = {"fixtureId": 500, "leagueCode": "E0", "season": 2026,
                        "home": "Arsenal", "away": "Brighton", "homeId": 42, "awayId": 51,
                        "homeGoals": 1, "awayGoals": 1, "date": self.final["date_utc"],
                        "venue": "Actual venue"}
        self.write("data/statmaker/domestic_enriched/index.json", {"schema_version": 3, "leagues": [self.entry]})
        self.write(self.entry["output_path"], {"competition": {"league_code": "E0"}, "matches": []})
        self.save_fast()

    def write(self, name, value):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))

    def save_fast(self):
        self.write("data/statmaker/priority_refresh/E0.json", self.doc)

    def events(self, code="E0", count=2):
        rows = [{"type": "Goal", "detail": "Normal Goal" if i == 0 else "Own Goal",
                 "team": {"id": 42 if i == 0 else 51, "name": "Provider alias"},
                 "player": {"name": "Player " + str(i)}, "time": {"elapsed": 10+i, "extra": 0}}
                for i in range(count)]
        self.write("data/api_football/schedule_scorers/events/fixture_500.json", {
            "fixtureId": 500, "leagueCode": code, "sourceRequest": "/fixtures/events?fixture=500",
            "retrievedAtUTC": NOW.isoformat(), "response": rows})
        return rows

    def test_fast_final_is_available_before_enriched_nightly_archive(self):
        leagues, fixtures = canonical_index(self.root)
        self.assertEqual(fixtures["500"]["totalGoals"], 2)
        with patch.object(context, "ROOT", self.root):
            self.assertEqual(context.current_fixtures()[0]["homeId"], 42)
        self.doc["season"] = 2025
        self.save_fast()
        self.assertEqual(list(priority_completed(self.root, leagues)), [])
        self.assertEqual(canonical_index(self.root)[1], {})

    def test_invalid_row_does_not_hide_other_verified_finals(self):
        self.doc["fixtures"].insert(0, dict(self.final, fixture_id=501, date_utc="invalid"))
        self.save_fast()
        self.assertEqual([f[2]["fixture_id"] for f in priority_completed(self.root, {"E0": self.entry})], [500])

    def test_leaders_refresh_at_half_hour_after_recent_final(self):
        self.write("data/api_football/schedule_scorers/leaders/E0_2026.json", {
            "sourceRequest": "/players/topscorers?league=39&season=2026", "season": 2026,
            "apiFootballLeagueId": 39, "retrievedAtUTC": (NOW-timedelta(hours=1)).isoformat(),
            "response": [{"player": {"name": "Player"}}]})
        with patch.object(leaders, "datetime") as clock:
            clock.now.return_value = NOW
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.assertEqual(len(leaders.plan(self.root, "leaders", 14)), 1)
            self.final["date_utc"] = (NOW-timedelta(days=1)).isoformat()
            self.save_fast()
            self.assertEqual(leaders.plan(self.root, "leaders", 14), [])

    def test_new_scorer_gap_ignores_old_morning_window_and_deduplicates(self):
        with patch.object(context, "CACHE", self.cache):
            self.assertTrue(context.watchdog_refresh_needed([self.fixture], {"refreshWindowKey": "morning-2026-10-26"}, NOW))
            self.assertFalse(context.watchdog_refresh_needed([self.fixture], {"watchdogLastAttemptAtUTC": (NOW+timedelta(minutes=1)).isoformat()}, NOW+timedelta(minutes=10)))

    def test_complete_cache_uses_team_ids_and_no_provider_calls(self):
        self.events()
        with patch.object(context, "CACHE", self.cache), patch.object(context, "http_get") as http:
            state = context.collect([self.fixture], 32, 1500)
            doc = context.build([self.fixture], state)
            self.assertEqual(state["calls"], 0)
            http.assert_not_called()
            self.assertEqual([g["team"] for g in doc["goalEvents"]], ["Arsenal", "Brighton"])
            self.assertTrue(doc["goalEvents"][1]["ownGoal"])
            self.assertEqual(doc["fixtureVenues"]["500"], "Actual venue")
            self.assertFalse(context.watchdog_refresh_needed([self.fixture], {}, NOW))

    def test_incomplete_or_wrong_league_cache_is_retried(self):
        for code, count in [("SP1", 2), ("E0", 1)]:
            self.events(code, count)
            with patch.object(context, "CACHE", self.cache), patch.object(context, "http_get", return_value=[]) as http:
                context.collect([self.fixture], 32, 1500)
                http.assert_called_once()

    def test_budget_stop_preserves_previous_verified_leaders(self):
        with patch.object(leaders, "get_data", side_effect=leaders.BudgetStop("daily cap")):
            result = leaders.execute(self.root, [("players/topscorers", {"league":39, "season":2026}, self.root/"unused")], 6, "test")
        self.assertEqual(result["cachedFiles"], 0)
        self.assertIn("shared_budget_stop", result["stoppedReason"])

    def uefa_docs(self, verified=0):
        for code, lid in [("CL", 2), ("EL", 3), ("UECL", 848)]:
            self.write("data/statmaker/league_tables/"+code+".json", {
                "contract": programme.CONTRACT, "leagueCode": code, "season": 2026,
                "apiFootballLeagueId": lid, "generatedAtUTC": "2026-10-20T12:00:00Z",
                "fixtures": [dict(self.final, date_utc="2026-10-20T19:00:00Z")] if code=="CL" else [],
                "scorers": {"verifiedFixtures": verified}})

    def test_uefa_old_gaps_retry_until_verified_and_qualifiers_do_not_leak(self):
        self.uefa_docs()
        self.assertTrue(due(self.root, "tables", NOW, watchdog=True)[0])
        self.uefa_docs(verified=1)
        self.write("data/statmaker/uefa_current_stats.json", {"competitions": [{"leagueCode":"CL", "season":2026, "phaseStart":"2026-09-01"}]})
        self.write("data/statmaker/priority_refresh/CL.json", dict(self.doc, leagueCode="CL", apiFootballLeagueId=2,
            fixtures=[dict(self.final, fixture_id=700, date_utc="2026-08-20T19:00:00Z")]))
        self.assertFalse(due(self.root, "tables", NOW, watchdog=True)[0])

    def test_uefa_only_preserves_entire_domestic_programme_without_stats_cache(self):
        row = {"leagueCode":"E0", "country":"England", "competition":"Premier League", "season":2026, "displaySeason":"2026-2027"}
        fixtures = [dict(self.final, fixture_id=500+i) for i in range(25)]
        self.write("data/statmaker/league_tables/index.json", {"leagues":[row]})
        self.write("data/statmaker/league_tables/E0.json", dict(self.doc, fixtures=fixtures))
        with patch.object(programme, "current_leagues", side_effect=AssertionError("must not load full stats")):
            index = programme.build(self.root, programme.Provider(self.root, False, 64, 1500), uefa_only=True)
        doc = json.loads((self.root/"data/statmaker/league_tables/E0.json").read_text())
        self.assertEqual(doc["fixtures"], fixtures)
        self.assertEqual(doc["generatedAtUTC"], index["generatedAtUTC"])

    def test_watchdog_routes_all_producers_with_existing_shared_budget(self):
        import yaml
        workflows = Path(__file__).resolve().parents[1]/".github/workflows"
        watchdog = yaml.safe_load((workflows/"live-settlement-watchdog.yml").read_text())
        jobs = watchdog["jobs"]
        self.assertEqual(jobs["refresh-scorer-leaderboards"]["needs"], "refresh-domestic-scorer-events")
        for name in ("refresh-domestic-scorer-events", "refresh-scorer-leaderboards", "refresh-uefa-scorers"):
            job = jobs[name]
            self.assertTrue(job["with"]["watchdog"])
            source = (workflows/Path(job["uses"]).name).read_text()
            self.assertIn("workflow_call:", source)
            self.assertIn("inputs.watchdog ||", source)
            self.assertIn('STATMAKER_API_BUDGET_ENFORCE: "true"', source)


if __name__ == "__main__":
    unittest.main()
