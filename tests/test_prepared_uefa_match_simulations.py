import json
import sqlite3
import unittest
from unittest.mock import patch

from scripts import materialize_prepared_uefa_simulations as uefa


class UefaMatchSimulationTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.executescript("""
            CREATE TABLE prepared_fixture_matches (
                competition_id TEXT,snapshot_version TEXT,match_key TEXT,
                season TEXT,kickoff TEXT,home_team TEXT,away_team TEXT);
            CREATE TABLE prepared_uefa_simulation_meta (
                competition_id TEXT,season TEXT,base_home_goals REAL,base_away_goals REAL,
                completed_league_matches INTEGER,generated_at_ms INTEGER,
                model_version TEXT,strength_model TEXT);
            CREATE TABLE prepared_uefa_team_simulations (
                competition_id TEXT,season TEXT,team_name TEXT,strength_rating REAL,
                current_played INTEGER,generated_at_ms INTEGER);
            CREATE TABLE prepared_match_simulations (
                competition_id TEXT,snapshot_version TEXT,match_key TEXT,
                simulation_runs INTEGER,simulation_model TEXT,elo_model_version TEXT,
                home_elo_rating REAL,away_elo_rating REAL,elo_weight REAL,
                home_win_probability REAL,draw_probability REAL,away_win_probability REAL,
                expected_home_goals REAL,expected_away_goals REAL,score_distribution_json TEXT,
                history_home_sample INTEGER,history_away_sample INTEGER,
                history_league_sample INTEGER,generated_at_ms INTEGER,
                PRIMARY KEY(competition_id,snapshot_version,match_key));
            CREATE TABLE prepared_selections (issued_contract TEXT);
            CREATE TABLE prepared_simulations (existing_probability REAL);
            INSERT INTO prepared_selections VALUES ('unchanged');
            INSERT INTO prepared_simulations VALUES (0.63);
        """)
        self.config = {"competitions": []}
        for cid in ("champions_league", "europa_league", "conference_league"):
            names = [f"{cid} Team {n}" for n in range(36)]
            self.config["competitions"].append({
                "competitionId": cid, "pots": [names], "potBaseRatings": [1500],
                "aliases": {"Provider Home": names[0], "Provider Away": names[1]},
            })
            self.con.execute("INSERT INTO prepared_uefa_simulation_meta VALUES(?,?,?,?,?,?,?,?)",
                             (cid, "2026-2027", 1.55, 1.3, 36, 1000,
                              uefa.MODEL_VERSION, uefa.STRENGTH_MODEL))
            self.con.executemany("INSERT INTO prepared_uefa_team_simulations VALUES(?,?,?,?,?,?)",
                                 [(cid, "2026-2027", n, 1600 - i * 4, 3, 1000)
                                  for i, n in enumerate(names)])
            self.fixture(cid, "valid", "Provider Home", "Provider Away")

    def fixture(self, cid, key, home, away, season="2026-2027",
                kickoff="2026-10-22T18:00:00Z"):
        self.con.execute("INSERT INTO prepared_fixture_matches VALUES(?,?,?,?,?,?,?)",
                         (cid, "snapshot", key, season, kickoff, home, away))

    def materialize(self, **kwargs):
        with patch.object(uefa, "api_get", side_effect=AssertionError("No API calls")):
            return uefa.materialize_match_simulations(self.con, self.config, now_ms=2000, **kwargs)

    def test_all_three_competitions_have_coherent_50000_draw_distributions(self):
        counts = self.materialize()
        self.assertEqual(set(counts.values()), {1})
        rows = self.con.execute("SELECT simulation_runs,simulation_model,elo_model_version,"
                                "home_win_probability,draw_probability,away_win_probability,"
                                "score_distribution_json FROM prepared_match_simulations").fetchall()
        self.assertEqual(len(rows), 3)
        for runs, model, strength, home, draw, away, raw in rows:
            self.assertEqual(runs, 50000)
            self.assertEqual(model, uefa.MATCH_MODEL_VERSION)
            self.assertEqual(strength, uefa.STRENGTH_MODEL)
            scores = json.loads(raw)
            self.assertAlmostEqual(sum(s["probability"] for s in scores), 1)
            self.assertAlmostEqual(home + draw + away, 1)
            for actual, predicate in ((home, lambda h, a: h > a),
                                      (draw, lambda h, a: h == a),
                                      (away, lambda h, a: h < a)):
                self.assertAlmostEqual(actual, sum(s["probability"] for s in scores
                    if predicate(s["homeGoals"], s["awayGoals"])))
        before = self.con.execute("SELECT * FROM prepared_match_simulations ORDER BY competition_id").fetchall()
        self.materialize()
        self.assertEqual(before, self.con.execute(
            "SELECT * FROM prepared_match_simulations ORDER BY competition_id").fetchall())
        self.assertEqual(self.con.execute("SELECT * FROM prepared_selections").fetchall(), [("unchanged",)])
        self.assertEqual(self.con.execute("SELECT * FROM prepared_simulations").fetchall(), [(0.63,)])

    def test_explicit_smaller_runs_retain_the_10000_draw_floor(self):
        self.materialize(runs=1000)
        self.assertEqual([(10000,)], self.con.execute(
            "SELECT DISTINCT simulation_runs FROM prepared_match_simulations").fetchall())

    def test_exact_aliases_only_and_same_season_and_pre_kickoff_snapshot(self):
        cid = "champions_league"
        self.fixture(cid, "unknown", "Provider Hom", "Provider Away")
        self.fixture(cid, "same-team", "Provider Home", "Provider Home")
        self.fixture(cid, "wrong-season", "Provider Home", "Provider Away", season="2025-2026")
        self.fixture(cid, "past", "Provider Home", "Provider Away", kickoff="1970-01-01T00:00:00Z")
        self.fixture(cid, "no-timezone", "Provider Home", "Provider Away", kickoff="2026-10-22T18:00:00")
        self.fixture(cid, "bad-date", "Provider Home", "Provider Away", kickoff="not-a-date")
        self.materialize()
        self.assertEqual(self.con.execute("SELECT match_key FROM prepared_match_simulations "
                                         "WHERE competition_id=?", (cid,)).fetchall(), [("valid",)])
        self.con.execute("UPDATE prepared_uefa_team_simulations SET generated_at_ms=2000000000000 "
                         "WHERE competition_id=?", (cid,))
        self.materialize()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM prepared_match_simulations "
                                         "WHERE competition_id=?", (cid,)).fetchone()[0], 0)

    def test_does_not_replace_existing_domestic_or_match_models(self):
        self.con.execute("INSERT INTO prepared_match_simulations(competition_id,snapshot_version,"
                         "match_key,simulation_model,home_win_probability) VALUES(?,?,?,?,?)",
                         ("domestic", "snapshot", "domestic-game", "match-monte-carlo-v2-elo", .7))
        self.con.execute("INSERT INTO prepared_match_simulations(competition_id,snapshot_version,"
                         "match_key,simulation_model,home_win_probability) VALUES(?,?,?,?,?)",
                         ("champions_league", "snapshot", "valid", "match-monte-carlo-v2-elo", .65))
        before = self.con.execute("SELECT * FROM prepared_match_simulations ORDER BY competition_id").fetchall()
        counts = self.materialize()
        self.assertEqual(counts["champions_league"], 0)
        after = self.con.execute("SELECT * FROM prepared_match_simulations "
                                 "WHERE simulation_model='match-monte-carlo-v2-elo' "
                                 "ORDER BY competition_id").fetchall()
        self.assertEqual(before, after)

    def test_incomplete_published_ratings_fail_instead_of_fabricating_strengths(self):
        self.con.execute("DELETE FROM prepared_uefa_team_simulations WHERE rowid=1")
        with self.assertRaisesRegex(ValueError, "Incomplete UEFA strength snapshot"):
            self.materialize()


if __name__ == "__main__":
    unittest.main()
