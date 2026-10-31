import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hotpatch_app_ready_simulation as hotpatch
import materialize_prepared_uefa_simulations as uefa
from season_simulation_audit import MATCH_TABLES, protected_digest, validate_precision, validate_match_precision
from simulation_precision import MATCH_SIMULATION_RUNS, SEASON_SIMULATION_RUNS


class SimulationPrecisionTest(unittest.TestCase):
    def test_cached_season_route_keeps_match_count_independent(self):
        with patch.object(hotpatch.subprocess, "run") as run:
            hotpatch.run_uefa_materializer(Path("snapshot.db"), cached=True, league_only=True)
        command = run.call_args.args[0]
        self.assertEqual("50000", command[command.index("--runs") + 1])
        self.assertEqual("50000", command[command.index("--match-runs") + 1])
        self.assertIn("--cached-league-tables", command)
        self.assertIn("--league-only", command)
        self.assertNotIn("--matches-only", command)

    def test_match_only_cli_does_not_inherit_the_season_count(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "snapshot.db"
            with sqlite3.connect(db):
                pass
            config = Path(td) / "uefa.json"
            config.write_text('{"competitions": []}')
            with patch.object(uefa, "CONFIG_PATH", config), patch.object(
                sys, "argv", ["uefa", str(db), "--matches-only", "--runs", "1000"]
            ), patch.object(uefa, "materialize_match_simulations", return_value={}) as materialize:
                self.assertEqual(0, uefa.main())
            self.assertEqual(MATCH_SIMULATION_RUNS, materialize.call_args.args[2])

    def test_conflicting_uefa_modes_fail_before_any_write(self):
        with patch.object(sys, "argv", ["uefa", "does-not-exist.db", "--matches-only", "--league-only"]):
            with self.assertRaises(SystemExit) as caught:
                uefa.main()
        self.assertEqual(2, caught.exception.code)

    def test_nonseason_data_is_protected_by_content_not_only_row_count(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "snapshot.db"
            with sqlite3.connect(db) as con:
                con.executescript("CREATE TABLE matches(id INTEGER PRIMARY KEY,goals INTEGER);"
                    "INSERT INTO matches VALUES(1,2);"
                    "CREATE TABLE prepared_league_simulation_meta(simulation_runs INTEGER);"
                    "INSERT INTO prepared_league_simulation_meta VALUES(10000);")
            before = protected_digest(db)
            with sqlite3.connect(db) as con:
                con.execute("UPDATE prepared_league_simulation_meta SET simulation_runs=50000")
            self.assertEqual(before, protected_digest(db))
            with sqlite3.connect(db) as con:
                con.execute("UPDATE matches SET goals=3 WHERE id=1")
            self.assertNotEqual(before, protected_digest(db))

    def database(self, folder):
        db = Path(folder) / "snapshot.db"
        with sqlite3.connect(db) as con:
            con.executescript("""
                CREATE TABLE prepared_league_simulation_meta(league_code TEXT,season TEXT,team_count INT,simulation_runs INT);
                CREATE TABLE prepared_league_team_simulations(league_code TEXT,season TEXT,title_probability REAL,position_probabilities_json TEXT);
                CREATE TABLE prepared_uefa_simulation_meta(competition_id TEXT,simulation_runs INT);
                CREATE TABLE prepared_uefa_team_simulations(competition_id TEXT,top8_probability REAL,playoff_probability REAL,round16_probability REAL,quarterfinal_probability REAL,semifinal_probability REAL,final_probability REAL,winner_probability REAL);
                INSERT INTO prepared_league_simulation_meta VALUES('G1','2026-2027',2,50000);
                INSERT INTO prepared_league_team_simulations VALUES('G1','2026-2027',0.8,'[0.8,0.2]');
                INSERT INTO prepared_league_team_simulations VALUES('G1','2026-2027',0.2,'[0.2,0.8]');
            """)
            for cid in ('champions_league','europa_league','conference_league'):
                con.execute("INSERT INTO prepared_uefa_simulation_meta VALUES(?,?)", (cid, SEASON_SIMULATION_RUNS))
                con.executemany("INSERT INTO prepared_uefa_team_simulations VALUES(?,?,?,?,?,?,?,?)",
                    [(cid, *[n/36 for n in (8,16,16,8,4,2,1)])] * 36)
        return db

    def test_complete_season_counts_and_probability_masses_are_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            proof = validate_precision(self.database(td), SEASON_SIMULATION_RUNS)
        self.assertEqual({'domesticLeagues':1,'uefaCompetitions':3,'seasonRuns':50000}, proof)

    def test_one_lower_precision_competition_cannot_hide_behind_maximum_count(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.database(td)
            with sqlite3.connect(db) as con:
                con.execute("UPDATE prepared_uefa_simulation_meta SET simulation_runs=10000 WHERE competition_id='europa_league'")
            with self.assertRaisesRegex(ValueError, "Incomplete season precision"):
                validate_precision(db, SEASON_SIMULATION_RUNS)

    def test_invalid_position_distribution_stops_publication(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.database(td)
            with sqlite3.connect(db) as con:
                con.execute("UPDATE prepared_league_team_simulations SET position_probabilities_json='[0.8,0.8]'")
            with self.assertRaises(ValueError):
                validate_precision(db, SEASON_SIMULATION_RUNS)

    def test_full_publisher_wires_season_and_match_counts_separately(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/app-ready-artifact-publisher.yml").read_text()
        import re
        for script in ('materialize_prepared_league_simulations.py','materialize_prepared_uefa_simulations.py'):
            self.assertRegex(workflow, re.escape(script) + r"[^\n]*\n[^\n]*\n\s*--runs 50000")
        self.assertIn("--runs 50000 --match-runs 50000", workflow)
        self.assertRegex(workflow, r"materialize_prepared_simulations.py[^\n]*\n[^\n]*\n\s*--runs 50000")


class MatchPrecisionAuditTest(unittest.TestCase):
    def database(self, folder):
        db = Path(folder) / "match.db"
        with sqlite3.connect(db) as con:
            con.executescript("""
                CREATE TABLE prepared_match_simulations(simulation_runs INT,simulation_model TEXT,
                    home_win_probability REAL,draw_probability REAL,away_win_probability REAL,score_distribution_json TEXT);
                CREATE TABLE prepared_match_metric_simulations(simulation_runs INT,
                    home_histogram_json TEXT,away_histogram_json TEXT,total_histogram_json TEXT);
                CREATE TABLE prepared_match_explorer_simulations(simulation_runs INT,
                    simulation_probability REAL,simulation_push_probability REAL);
                CREATE TABLE prepared_simulations(simulation_runs INT,
                    simulation_probability REAL,simulation_push_probability REAL);
                CREATE TABLE prepared_score_edge_values(simulation_runs INT);
                CREATE TABLE prepared_league_simulation_meta(simulation_runs INT);
                CREATE TABLE prepared_matches(id INT PRIMARY KEY,home_goals INT);
                INSERT INTO prepared_matches VALUES(1,2);
                INSERT INTO prepared_league_simulation_meta VALUES(50000);
                INSERT INTO prepared_score_edge_values VALUES(50000);
                INSERT INTO prepared_simulations VALUES(50000,0.6,0.1);
                INSERT INTO prepared_match_explorer_simulations VALUES(50000,0.6,0.1);
                INSERT INTO prepared_match_metric_simulations VALUES(50000,'[[0,0.4],[1,0.6]]','[[0,1]]','[[0,0.4],[1,0.6]]');
            """)
            scores = json.dumps([{"homeGoals":1,"awayGoals":0,"probability":0.6},
                                 {"homeGoals":0,"awayGoals":0,"probability":0.4}])
            con.execute("INSERT INTO prepared_match_simulations VALUES(50000,'uefa-match-monte-carlo-v1',0.6,0.4,0,?)", (scores,))
        return db

    def test_real_match_and_metric_distributions_at_50000_are_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            proof = validate_match_precision(self.database(td), MATCH_SIMULATION_RUNS)
        self.assertEqual({"matchRuns":50000,"matches":1,"uefaMatches":1,"metricHistograms":1}, proof)

    def test_one_old_metric_row_cannot_hide_behind_50000_summary(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.database(td)
            with sqlite3.connect(db) as con:
                con.execute("UPDATE prepared_match_metric_simulations SET simulation_runs=10000")
            with self.assertRaisesRegex(ValueError, "Incomplete match precision"):
                validate_match_precision(db, MATCH_SIMULATION_RUNS)

    def test_invalid_metric_mass_blocks_publication(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.database(td)
            with sqlite3.connect(db) as con:
                con.execute("UPDATE prepared_match_metric_simulations SET total_histogram_json='[[0,0.4]]'")
            with self.assertRaisesRegex(ValueError, "metric histogram"):
                validate_match_precision(db, MATCH_SIMULATION_RUNS)

    def test_score_distribution_must_agree_with_1x2(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.database(td)
            with sqlite3.connect(db) as con:
                con.execute("UPDATE prepared_match_simulations SET home_win_probability=0.4,draw_probability=0.6")
            with self.assertRaisesRegex(ValueError, "disagree"):
                validate_match_precision(db, MATCH_SIMULATION_RUNS)

    def test_match_refresh_protects_seasons_and_factual_history(self):
        with tempfile.TemporaryDirectory() as td:
            db = self.database(td)
            before = protected_digest(db, MATCH_TABLES)
            with sqlite3.connect(db) as con:
                con.execute("UPDATE prepared_match_metric_simulations SET simulation_runs=10000")
            self.assertEqual(before, protected_digest(db, MATCH_TABLES))
            with sqlite3.connect(db) as con:
                con.execute("UPDATE prepared_league_simulation_meta SET simulation_runs=10000")
            self.assertNotEqual(before, protected_digest(db, MATCH_TABLES))


if __name__ == "__main__":
    unittest.main()
