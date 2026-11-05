import copy
import json
import sqlite3
import shutil
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
        # The tests below construct a new provider table. Projection metadata
        # describes the live fixture results and must not carry over.
        cls.source["standings"] = cls.source.pop("officialStandings", cls.source["standings"])
        cls.source.pop("standingsSource", None)
        for rank, row in enumerate(cls.source["standings"], 1):
            row["rank"] = rank
        # Keep the schedule and identities; use one known result for unit tests
        # that must run even while the live provider is catching up.
        first = min(cls.source["fixtures"], key=lambda f: (f["date_utc"], f["fixture_id"]))
        for f in cls.source["fixtures"]:
            f.update(status="NS", home_goals=None, away_goals=None)
        first.update(status="FT", home_goals=2, away_goals=1)
        aliases, pots, _ = sim.participant_maps(cls.comp)
        home, away = (sim.resolve_team(first[k], aliases, list(pots)) for k in ("home_team", "away_team"))
        table = {team: sim.TableRow() for team in pots}
        sim.apply_result(table, home, away, 2, 1)
        for row in cls.source["standings"]:
            t = table[sim.resolve_team(row["team"], aliases, list(pots))]
            row.update(played=t.played, points=t.points, goalsFor=t.gf, goalsAgainst=t.ga)

    def cache(self, source):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = root / "data/statmaker/league_tables/UECL.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(source))
        for relative in ("config/uefa_clubelo_aliases.json", "data/statmaker/uefa_clubelo_strengths.json"):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sim.ROOT / relative, target)
        (root / "data/statmaker/domestic_enriched").mkdir()
        (root / "data/statmaker/domestic_enriched/index.json").write_text('{"leagues":[]}')
        (root / "data/statmaker/uefa_current_stats.json").write_text('{"competitions":[]}')
        return root

    def test_all_published_competitions_share_the_tables_results_and_ranks_without_api(self):
        with patch.object(sim, "api_get", side_effect=AssertionError("No provider calls")):
            pending = False
            for c in self.config["competitions"]:
                with self.subTest(competition=c["competitionId"]):
                    try:
                        fixtures, _, ranks, _ = sim.cached_competition(c, int(self.config["season"]))
                    except sim.UefaStandingsPending:
                        pending = True
                        continue
                    self.assertEqual(36, len(ranks))
                    self.assertEqual(int(c["expectedLeaguePhaseFixtures"]), len(fixtures))
            if pending:
                with self.assertRaises(sim.UefaStandingsPending):
                    sim.cached_source_fingerprint(self.config)
            else:
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
        with self.assertRaisesRegex(ValueError, "standings disagree") as error:
            sim.cached_competition(self.comp, int(self.config["season"]), self.cache(source))
        self.assertNotIsInstance(error.exception, sim.UefaStandingsPending)

    def test_verified_final_with_coherent_older_standings_defers_until_caught_up(self):
        source = copy.deepcopy(self.source)
        for row in source["standings"]:
            row.update(played=0, points=0, goalsFor=0, goalsAgainst=0)
        root = self.cache(source)
        season = int(self.config["season"])
        with self.assertRaises(sim.UefaStandingsPending):
            sim.cached_competition(self.comp, season, root)
        # A table that includes a match for one team only is inconsistent.
        index = next(i for i, row in enumerate(self.source["standings"]) if row["played"] == 1)
        source["standings"][index] = self.source["standings"][index]
        path = root / "data/statmaker/league_tables/UECL.json"
        path.write_text(json.dumps(source))
        with self.assertRaisesRegex(ValueError, "inclusion of fixture"):
            sim.cached_competition(self.comp, season, root)
        path.write_text(json.dumps(self.source))
        self.assertEqual(36, len(sim.cached_competition(self.comp, season, root)[2]))

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
        root = self.cache(self.source)
        fixtures, _, ranks, _ = sim.cached_competition(c, int(self.config["season"]), root)
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
        self.assertEqual(36, sim.verify_cached_simulations(con, one, root)[c["competitionId"]]["teams"])
        team = "Panathinaikos"
        con.execute("UPDATE prepared_uefa_team_simulations SET current_position=? WHERE team_name=?",
                    (12 if ranks[team] != 12 else 1, team))
        with self.assertRaisesRegex(ValueError, "stale simulation current standing"):
            sim.verify_cached_simulations(con, one, root)

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
