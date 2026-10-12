"""Verified prior-season carryover: never gate UEFA on UEFA-only games."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "betting_v2"))
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.betting_v2.uefa_domestic_elo_bridge import UefaDomesticEloBridge


class UefaDomesticEloBridgeTest(unittest.TestCase):
    def test_last_season_elo_and_domestic_matches_work_without_uefa_games(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "ready.sqlite"
            with sqlite3.connect(db) as con:
                con.execute(
                    "CREATE TABLE prepared_team_elo (league_code TEXT, season TEXT,"
                    "team_name TEXT, elo_rating REAL, previous_rating REAL,"
                    "historical_matches INT, current_matches INT)"
                )
                con.executemany(
                    "INSERT INTO prepared_team_elo VALUES (?,?,?,?,?,?,?)",
                    [
                        ("E0", "2026-2027", "Arsenal", 1634., 1628., 32, 6),
                        ("P1", "2026-2027", "Sporting CP", 1598., 1594., 30, 5),
                    ],
                )
            completed = []
            base = datetime(2026, 8, 1, tzinfo=timezone.utc)
            for i in range(12):
                for team, code in (("Arsenal", "E0"), ("Sporting CP", "P1")):
                    home = i % 2 == 0
                    completed.append({
                        "group": code+"|League", "fixture_id": code+str(i),
                        "date": base + timedelta(days=i*3),
                        "home": team.lower() if home else "opponent "+code+str(i),
                        "away": "opponent "+code+str(i) if home else team.lower(),
                        "hg": 2 if home else 1,
                        "ag": 0 if home else 1,
                        "stats": {"HC": 6, "AC": 3, "HS": 14, "AS": 8,
                                  "HST": 5, "AST": 2, "HY": 2, "AY": 1},
                    })
            now = datetime(2026, 10, 12, tzinfo=timezone.utc)
            bridge = UefaDomesticEloBridge(db, root, completed, now)
            fixture = {
                "leagueCode": "CL", "homeTeam": "Arsenal", "awayTeam": "Sporting CP",
                "kickoff": "2026-10-15T20:00:00Z",
            }
            estimate = bridge.forecast(fixture)
            self.assertIsNotNone(estimate)
            h, a, evidence = estimate
            self.assertGreater(h, 0)
            self.assertGreater(a, 0)
            self.assertEqual(evidence["homeElo"], 1634.)
            self.assertEqual(evidence["awayElo"], 1598.)
            self.assertEqual(evidence["homeHistoricalMatches"], 32)
            self.assertFalse(evidence["certified"])
            self.assertIsNotNone(
                bridge.metric_rates(fixture, "CORNERS", evidence,
                                    datetime(2026, 10, 15, tzinfo=timezone.utc))
            )

    def test_unknown_teams_do_not_receive_fabricated_elo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "ready.sqlite"
            with sqlite3.connect(db) as con:
                con.execute(
                    "CREATE TABLE prepared_team_elo (league_code TEXT, season TEXT,"
                    "team_name TEXT, elo_rating REAL, previous_rating REAL,"
                    "historical_matches INT, current_matches INT)"
                )
                con.execute("INSERT INTO prepared_team_elo VALUES (?,?,?,?,?,?,?)",
                            ("E0", "2026-2027", "Arsenal", 1634., 1628., 32, 6))
            bridge = UefaDomesticEloBridge(
                db, root, [],
                datetime(2026, 10, 12, tzinfo=timezone.utc)
            )
            self.assertIsNone(bridge.forecast({
                "leagueCode": "CL", "homeTeam": "Arsenal",
                "awayTeam": "Unknown Club", "kickoff": "2026-10-15T20:00:00Z"
            }))


if __name__ == "__main__":
    unittest.main()
