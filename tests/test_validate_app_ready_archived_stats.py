"""Strict regression tests for expired rollover-history scopes."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.reconcile_app_ready_stats_scope import canonical_key
from scripts.validate_app_ready_archived_stats import validated_archived_scope_counts


class ArchivedScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archives = self.root / "data/statmaker/domestic_enriched"
        self.archives.mkdir(parents=True)
        self.db = self.root / "statmaker.db"
        self.fixture = {
            "fixture_id": 101, "date_utc": "2025-08-02T14:00:00+00:00",
            "app_season": "2025-2026", "league_code": "SC0",
            "home_team": "Kilmarnock", "away_team": "Livingston",
        }
        self.path = self.archives / "scotland_premiership_2025.json"
        self.write_archive([self.fixture])
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE matches (match_key TEXT, season TEXT, division TEXT, "
                "date_text TEXT, home_team TEXT, away_team TEXT)"
            )
            self.insert(con, self.fixture)
        self.index = {"leagues": [{"league_code": "SC0", "app_season": "2026-2027",
                                    "completed_fixtures": 42}]}

    def write_archive(self, matches):
        self.path.write_text(json.dumps({
            "competition": {"league_code": "SC0", "app_season": "2025-2026",
                            "api_football_season": "2025"},
            "source": {"provider": "api-football"},
            "readiness": {"completed_fixtures": len(matches)},
            "matches": matches,
        }))

    def insert(self, con, fixture, key=None):
        con.execute(
            "INSERT INTO matches VALUES(?,?,?,?,?,?)",
            (key or canonical_key("2526", "SC0", fixture),
             "2526", "SC0", fixture["date_utc"][:10],
             fixture["home_team"], fixture["away_team"]),
        )

    def check(self, actual=None, expected=None):
        return validated_archived_scope_counts(
            self.db, self.index, self.root, actual or {("2526", "SC0"): 1},
            expected if expected is not None else {("2026-2027", "SC0"): 42},
        )

    def test_accept_verified_archived_scope_without_mutation(self):
        self.assertEqual(self.check(), {("2526", "SC0"): 1})
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 1)

    def test_reject_noncanonical_orphan(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE matches SET match_key='2526|sc0|2025-08-02|ghost|livingston'")
        with self.assertRaisesRegex(ValueError, "exact-key mismatch"):
            self.check()

    def test_reject_absent_archived_source(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "Unverified archived scope"):
            self.check()

    def test_reject_duplicates_in_source(self):
        self.write_archive([self.fixture, self.fixture])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.check()

    def test_reject_provider_mismatch(self):
        source = json.loads(self.path.read_text())
        source["source"]["provider"] = "unknown"
        self.path.write_text(json.dumps(source))
        with self.assertRaisesRegex(ValueError, "provider"):
            self.check()

    def test_current_canonical_scope_not_overridden(self):
        self.assertEqual(self.check(expected={("2526", "SC0"): 1}), {})

    def test_reject_non_archival_extra(self):
        with self.assertRaisesRegex(ValueError, "non-archival"):
            self.check(actual={("2026-2027", "SC0"): 1}, expected={})


if __name__ == "__main__":
    unittest.main()
