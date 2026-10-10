"""Synthetic local Git-history -> archived SQLite ZIP replay; no network or Actions."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from archive_preflight import inspect_day


class BettingV2ArchiveGitReplayTests(unittest.TestCase):
    def test_archive_commit_manifest_zip_and_sqlite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def git(*args: str, env: dict | None = None) -> str:
                return subprocess.run(
                    ["git", *args], cwd=root, check=True, env=env,
                    capture_output=True, text=True
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.email", "tester@example.invalid")
            git("config", "user.name", "Synthetic Test")
            db = root / "sample.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE prepared_matches (competition_id TEXT, "
                            "snapshot_version TEXT, match_key TEXT, payload TEXT)")
                con.execute("CREATE TABLE prepared_selections (competition_id TEXT, "
                            "snapshot_version TEXT, match_key TEXT, selection_key TEXT, "
                            "selection_odd REAL, identity_sub_market_key TEXT, "
                            "identity_selection_side TEXT, identity_team_side TEXT, "
                            "identity_line REAL)")
                match = {
                    "homeTeam": "A", "awayTeam": "B", "leagueCode": "E0",
                    "kickoff": "2026-10-10T18:00:00Z",
                    "squadContext": {"apiFootballFixtureId": 777}
                }
                con.execute("INSERT INTO prepared_matches VALUES (?,?,?,?)",
                            ("E0", "s", "match1", json.dumps(match)))
                con.execute(
                    "INSERT INTO prepared_selections VALUES (?,?,?,?,?,?,?,?,?)",
                    ("E0", "s", "match1", "selection-1", 2.2,
                     "RESULT_1X2", "HOME", None, None))
            archive_rel = "data/statmaker/app_ready/synthetic.zip"
            archive = root / archive_rel
            archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(db, "databases/statmaker_prepared_betting.db")
            manifest = {
                "profile": "app_ready",
                "generatedAt": "2026-10-10T07:40:00Z",
                "artifacts": [{
                    "id": "app_ready_betting_bundle",
                    "path": archive_rel,
                    "sha256": sha256(archive.read_bytes()).hexdigest(),
                    "generatedAt": "2026-10-10T07:20:00Z"
                }]
            }
            (archive.parent / "update_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8")
            git("add", ".")
            environment = dict(os.environ)
            environment.update(
                GIT_AUTHOR_DATE="2026-10-10T07:50:00Z",
                GIT_COMMITTER_DATE="2026-10-10T07:50:00Z"
            )
            git("commit", "-qm", "synthetic as-of archive", env=environment)

            report = inspect_day(root, date(2026, 10, 10))
            self.assertEqual(report["status"], "DIAGNOSTIC_ONLY", report)
            self.assertEqual(report["preparedSelectionRows"], 1)
            self.assertEqual(report["eligibleBeforeExactFixtureJoin"], 1)
            self.assertEqual(report["eligibleWithExplicitFixtureId"], 1)
            self.assertFalse(report["certified"])
            self.assertFalse(report["actualPriceObservationTimeVerified"])


if __name__ == "__main__":
    unittest.main()
