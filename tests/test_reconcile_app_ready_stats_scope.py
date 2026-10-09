"""Regression: reject missing canonical matches; prune only stale checkpoint rows."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/reconcile_app_ready_stats_scope.py"
SPEC = importlib.util.spec_from_file_location("reconcile_app_ready_stats_scope", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ReconcileAppReadyStatsScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "data/statmaker/domestic_enriched/france_ligue_1_2025.json"
        self.source.parent.mkdir(parents=True)
        self.index = self.root / "domestic_enriched_index.json"
        self.database = self.root / "statmaker.db"
        self.canonical = [
            {"fixture_id": i, "date_utc": f"2025-08-{i:02d}T12:00:00Z",
             "home_team": f"Team{i}", "away_team": f"Opponent{i}"}
            for i in range(1, 4)
        ]
        self.save_canonical()
        with sqlite3.connect(self.database) as conn:
            conn.execute("""
                CREATE TABLE matches (
                    id INTEGER PRIMARY KEY, match_key TEXT NOT NULL UNIQUE,
                    season TEXT, division TEXT, date_text TEXT,
                    home_team TEXT, away_team TEXT
                )
            """)
            for m in self.canonical:
                self.insert_match(conn, m)
            self.insert_match(conn, {"date_utc": "2025-09-01", "home_team": "Ghost",
                                     "away_team": "A"})
            self.insert_match(conn, {"date_utc": "2025-09-02", "home_team": "Ghost",
                                     "away_team": "B"})
            self.insert_match(conn, {"date_utc": "2025-09-03", "home_team": "Other",
                                     "away_team": "League"}, season="2526", league="F2")
            conn.commit()

    def save_canonical(self) -> None:
        self.source.write_text(json.dumps({"matches": self.canonical}), encoding="utf-8")
        self.index.write_text(json.dumps({"leagues": [{
            "league_code": "F1",
            "app_season": "2025-2026",
            "completed_fixtures": len(self.canonical),
            "output_path": "data/statmaker/domestic_enriched/france_ligue_1_2025.json"
        }]}), encoding="utf-8")

    def insert_match(self, conn, m, season="2526", league="F1") -> None:
        date = m["date_utc"][:10]
        key = module.canonical_key(season, league, m)
        conn.execute(
            "INSERT INTO matches(match_key,season,division,date_text,home_team,away_team) "
            "VALUES(?,?,?,?,?,?)",
            (key, season, league, date, m["home_team"], m["away_team"])
        )

    def count(self, code="F1") -> int:
        with sqlite3.connect(self.database) as conn:
            return conn.execute("SELECT COUNT(*) FROM matches WHERE division=?", (code,)).fetchone()[0]

    def run_reconcile(self) -> tuple[int, int]:
        return module.reconcile(self.database, self.index, self.root)

    def test_only_orphaned_f1_rows_removed(self) -> None:
        self.assertEqual(self.run_reconcile(), (2, 3))
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.count("F2"), 1)
        self.assertEqual(self.run_reconcile(), (0, 3))

    def test_missing_canonical_key_never_deletes_anything(self) -> None:
        with sqlite3.connect(self.database) as conn:
            key = module.canonical_key("2526", "F1", self.canonical[0])
            conn.execute("DELETE FROM matches WHERE match_key=?", (key,))
        with self.assertRaisesRegex(ValueError, "canonical matches MISSING"):
            self.run_reconcile()
        self.assertEqual(self.count(), 4)
        self.assertEqual(self.count("F2"), 1)

    def test_incomplete_canonical_artifact_never_prunes(self) -> None:
        self.canonical.pop()
        # Keep index count at 3: the actual artifact is incomplete.
        self.source.write_text(json.dumps({"matches": self.canonical}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.run_reconcile()
        self.assertEqual(self.count(), 5)

    def test_duplicate_canonical_keys_never_prunes(self) -> None:
        self.canonical[2] = dict(self.canonical[1])
        self.save_canonical()
        with self.assertRaisesRegex(ValueError, "Duplicate canonical"):
            self.run_reconcile()
        self.assertEqual(self.count(), 5)


if __name__ == "__main__":
    unittest.main()
