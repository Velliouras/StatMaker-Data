import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import materialize_prepared_uefa_simulations as sim


class CachedUefaSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(sim.CONFIG_PATH.read_text())
        cls.comp = next(c for c in cls.config["competitions"] if c["competitionId"] == "conference_league")
        cls.source = json.loads((sim.ROOT / "data/statmaker/league_tables/UECL.json").read_text())

    def cache(self, source):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = root / "data/statmaker/league_tables/UECL.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(source))
        return root

    def test_all_published_competitions_share_the_tables_results_and_ranks_without_api(self):
        with patch.object(sim, "api_get", side_effect=AssertionError("No provider calls")):
            for c in self.config["competitions"]:
                fixtures, _, ranks, _ = sim.cached_competition(c, int(self.config["season"]))
                self.assertEqual(36, len(ranks))
                self.assertEqual(int(c["expectedLeaguePhaseFixtures"]), len(fixtures))
            self.assertEqual(64, len(sim.cached_source_fingerprint(self.config)))

    def test_rejects_wrong_season_duplicate_missing_and_unscored_finished_fixtures(self):
        cases = []
        wrong = copy.deepcopy(self.source); wrong["season"] -= 1; cases.append(wrong)
        duplicate = copy.deepcopy(self.source); duplicate["fixtures"].append(duplicate["fixtures"][0]); cases.append(duplicate)
        missing = copy.deepcopy(self.source); missing["fixtures"].pop(); cases.append(missing)
        score = copy.deepcopy(self.source)
        next(f for f in score["fixtures"] if f["status"] in sim.FINISHED)["home_goals"] = None
        cases.append(score)
        for source in cases:
            with self.subTest(case=len(source["fixtures"])), self.assertRaises(ValueError):
                sim.cached_competition(self.comp, int(self.config["season"]), self.cache(source))

    def test_rejects_standings_that_do_not_match_completed_results(self):
        source = copy.deepcopy(self.source)
        source["standings"][0]["points"] += 1
        with self.assertRaisesRegex(ValueError, "standings disagree"):
            sim.cached_competition(self.comp, int(self.config["season"]), self.cache(source))

    def test_finished_results_ahead_of_schedule_are_rejected(self):
        root = self.cache(self.source)
        f = next(f for f in self.source["fixtures"] if f["status"] in sim.FINISHED)
        stats = {"season": self.config["season"], "competitions": [{
            "competitionId": "conference_league", "fixtures": [{
                "fixtureId": f["fixture_id"], "status": "FT",
                "homeGoals": f["home_goals"] + 1, "awayGoals": f["away_goals"]}]}]}
        (root / "data/statmaker/uefa_current_stats.json").write_text(json.dumps(stats))
        with self.assertRaisesRegex(ValueError, "behind published"):
            sim.cached_competition(self.comp, int(self.config["season"]), root)

    def test_validation_rejects_old_current_position_then_accepts_current_tables(self):
        c = self.comp
        fixtures, _, ranks, _ = sim.cached_competition(c, int(self.config["season"]))
        table = {t: sim.TableRow() for t in ranks}
        for f in fixtures:
            if f.status in sim.FINISHED:
                sim.apply_result(table, f.home, f.away, f.home_goals, f.away_goals)
        completed = sum(f.status in sim.FINISHED for f in fixtures)
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE prepared_uefa_simulation_meta(competition_id,season,"
                    "completed_league_matches,remaining_league_matches,simulation_runs)")
        con.execute("INSERT INTO prepared_uefa_simulation_meta VALUES(?,?,?,?,?)",
                    (c["competitionId"], "2026-2027", completed, len(fixtures)-completed, 10000))
        con.execute("CREATE TABLE prepared_uefa_team_simulations(competition_id,season,team_name,"
                    "current_position,current_played,current_points,current_goal_difference,current_goals_for)")
        con.executemany("INSERT INTO prepared_uefa_team_simulations VALUES(?,?,?,?,?,?,?,?)",
            [(c["competitionId"], "2026-2027", t, ranks[t], row.played, row.points, row.gd, row.gf)
             for t, row in table.items()])
        one = {"season": self.config["season"], "competitions": [c]}
        self.assertEqual(36, sim.verify_cached_simulations(con, one)[c["competitionId"]]["teams"])
        team = "Panathinaikos"
        con.execute("UPDATE prepared_uefa_team_simulations SET current_position=? WHERE team_name=?",
                    (12 if ranks[team] != 12 else 1, team))
        with self.assertRaisesRegex(ValueError, "stale simulation current standing"):
            sim.verify_cached_simulations(con, one)

    def test_fingerprint_ignores_publication_timestamps(self):
        source = copy.deepcopy(self.source)
        root = self.cache(source)
        one = {"season": self.config["season"], "competitions": [self.comp]}
        before = sim.cached_source_fingerprint(one, root)
        source["generatedAtUTC"] = "2099-01-01T00:00:00Z"
        (root / "data/statmaker/league_tables/UECL.json").write_text(json.dumps(source))
        self.assertEqual(before, sim.cached_source_fingerprint(one, root))


if __name__ == "__main__":
    unittest.main()
