"""Offline synthetic archive schema and time-window checks; no provider requests."""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from archive_preflight import examine_database


class BettingV2ArchivePreflightTests(unittest.TestCase):
    @staticmethod
    def fixture_blob(kickoff: str, odds: tuple[float, ...],
                     match_changes: dict | None = None) -> bytes:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "archive.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE prepared_matches (competition_id TEXT, "
                "snapshot_version TEXT, match_key TEXT, payload TEXT)")
            conn.execute(
                "CREATE TABLE prepared_selections (competition_id TEXT, "
                "snapshot_version TEXT, match_key TEXT, selection_key TEXT, "
                "selection_odd REAL, identity_sub_market_key TEXT, "
                "identity_selection_side TEXT, identity_team_side TEXT, "
                "identity_line REAL)")
            match = {
                "kickoff": kickoff, "leagueCode": "E0",
                "homeTeam": "Home FC", "awayTeam": "Away FC",
                "squadContext": {"apiFootballFixtureId": 12345}
            }
            match.update(match_changes or {})
            conn.execute("INSERT INTO prepared_matches VALUES (?,?,?,?)",
                         ("E0", "v1", "fixture", json.dumps(match)))
            for index, odd in enumerate(odds):
                conn.execute(
                    "INSERT INTO prepared_selections VALUES (?,?,?,?,?,?,?,?,?)",
                    ("E0", "v1", "fixture", f"selection-{index}", odd,
                     "RESULT_1X2", "HOME", None, None))
            conn.commit()
            conn.close()
            return db_path.read_bytes()

    @staticmethod
    def examine(blob: bytes) -> dict:
        return examine_database(
            blob, date(2026, 10, 10),
            datetime(2026, 10, 10, 8, tzinfo=timezone.utc))

    def test_strict_window_and_unverified_source(self):
        report = self.examine(self.fixture_blob(
            "2026-10-10T18:00:00Z", (2.25, 1.80, 3.00)))
        self.assertEqual(report["eligibleBeforeExactFixtureJoin"], 1)
        self.assertEqual(report["eligibleWithExplicitFixtureId"], 1)
        self.assertEqual(report["excludedReasons"]["OUTSIDE_V2_ODDS_WINDOW"], 2)
        self.assertFalse(report["certified"])
        self.assertFalse(report["actualPriceObservationTimeVerified"])
        self.assertFalse(report["independentUnfilteredBookmakerUniverseVerified"])

    def test_time_only_kickoff_fails_closed(self):
        report = self.examine(self.fixture_blob("18:00", (2.25,)))
        self.assertEqual(report["eligibleBeforeExactFixtureJoin"], 0)
        self.assertEqual(report["excludedReasons"]["UNKNOWN_ABSOLUTE_KICKOFF"], 1)

    def test_already_started_fixture_fails_closed(self):
        report = self.examine(self.fixture_blob("2026-10-10T07:59:00Z", (2.25,)))
        self.assertEqual(report["eligibleBeforeExactFixtureJoin"], 0)
        self.assertEqual(report["excludedReasons"]["MATCH_ALREADY_STARTED"], 1)

    def test_no_fixture_id_is_not_claimed_verified(self):
        report = self.examine(self.fixture_blob(
            "2026-10-10T18:00:00Z", (2.25,),
            match_changes={"squadContext": None}))
        self.assertEqual(report["eligibleBeforeExactFixtureJoin"], 1)
        self.assertEqual(report["eligibleWithExplicitFixtureId"], 0)

    def test_malformed_blob_fails_closed(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.examine(b"not a SQLite file")


if __name__ == "__main__":
    unittest.main()
