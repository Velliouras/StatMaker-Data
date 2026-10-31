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
from season_simulation_audit import protected_digest, validate_precision
from simulation_precision import MATCH_SIMULATION_RUNS, SEASON_SIMULATION_RUNS


class SimulationPrecisionTest(unittest.TestCase):
    def test_cached_season_route_keeps_match_count_independent(self):
        with patch.object(hotpatch.subprocess, "run") as run:
            hotpatch.run_uefa_materializer(Path("snapshot.db"), cached=True, league_only=True)
        command = run.call_args.args[0]
        self.assertEqual("50000", command[command.index("--runs") + 1])
        self.assertEqual("10000", command[command.index("--match-runs") + 1])
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
                sys, "argv", ["uefa", str(db), "--matches-only", "--runs", "50000"]
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
        self.assertIn("--runs 50000 --match-runs 10000", workflow)


if __name__ == "__main__":
    unittest.main()
