"""Regression: reject missing canonical matches; prune only stale checkpoint rows."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import os
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

    def retire_scope(self) -> None:
        self.index.write_text(json.dumps({"leagues": [{
            "league_code": "F1", "app_season": "2026-2027", "completed_fixtures": 72
        }]}))
        self.source.write_text(json.dumps({
            "competition": {"league_code": "F1", "app_season": "2025-2026", "api_football_season": "2025"},
            "source": {"provider": "api-football"},
            "readiness": {"completed_fixtures": len(self.canonical)},
            "matches": self.canonical,
        }))

    def test_retired_scope_uses_verified_archive_and_preserves_other_history(self) -> None:
        self.retire_scope()
        self.assertEqual(self.run_reconcile(), (2, 3))
        self.assertEqual(self.count('F2'), 1)
        self.assertEqual(self.run_reconcile(), (0, 3))

    def test_retired_scope_missing_canonical_match_never_prunes(self) -> None:
        self.retire_scope()
        with sqlite3.connect(self.database) as conn:
            conn.execute('DELETE FROM matches WHERE match_key=?',
                         (module.canonical_key('2526', 'F1', self.canonical[0]),))
        with self.assertRaisesRegex(ValueError, 'canonical matches MISSING'):
            self.run_reconcile()
        self.assertEqual(self.count(), 4)

    def test_unverified_archive_never_prunes(self) -> None:
        self.retire_scope()
        payload = json.loads(self.source.read_text())
        payload['source']['provider'] = 'unverified'
        self.source.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, 'provider'):
            self.run_reconcile()
        self.assertEqual(self.count(), 5)

    def test_ambiguous_archive_never_prunes(self) -> None:
        self.retire_scope()
        self.source.with_name('duplicate_2025.json').write_text(self.source.read_text())
        with self.assertRaisesRegex(ValueError, 'source artifacts=2'):
            self.run_reconcile()
        self.assertEqual(self.count(), 5)

    def test_active_scope_is_not_replaced_by_an_archive_when_incomplete(self) -> None:
        self.retire_scope()
        self.index.write_text(json.dumps({'leagues': [{
            'league_code': 'F1', 'app_season': '2025-2026', 'completed_fixtures': 0,
            'output_path': 'data/statmaker/domestic_enriched/france_ligue_1_2025.json'
        }]}))
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.run_reconcile()
        self.assertEqual(self.count(), 5)

    def test_source_preflight_requires_no_checkpoint_database(self) -> None:
        self.retire_scope()
        missing_db = self.root / 'not-built-yet.db'
        result = subprocess.run([
            'python', str(MODULE_PATH), str(missing_db), str(self.index),
            '--repo-root', str(self.root), '--validate-source-only',
        ], env=dict(os.environ, PYTHONPATH=''),
           capture_output=True, text=True, check=True)
        self.assertIn('APP_READY_STATS_SOURCE_VALIDATED scope=F1@2526 origin=archive matches=3', result.stdout)
        self.assertFalse(missing_db.exists())

    def test_season_conversion_is_not_fixed_to_old_seasons(self) -> None:
        # Preserve Android's actual schema: legacy seasons use compact keys,
        # while newer seasons retain the full label.
        self.assertEqual(module.db_season('2026-2027'), '2026-2027')
        self.assertEqual(module.db_season('2027-2028'), '2027-2028')
        self.assertEqual(module.db_season('2026'), '2026')

    def test_absent_retired_scope_requires_no_pruning_or_import(self) -> None:
        self.retire_scope()
        with sqlite3.connect(self.database) as conn:
            conn.execute("DELETE FROM matches WHERE division='F1'")
        self.assertEqual(self.run_reconcile(), (0, 0))
        self.assertEqual(self.count('F2'), 1)

    def test_absent_active_scope_still_fails(self) -> None:
        with sqlite3.connect(self.database) as conn:
            conn.execute("DELETE FROM matches WHERE division='F1'")
        with self.assertRaisesRegex(ValueError, 'canonical matches MISSING'):
            self.run_reconcile()

    def test_later_retired_season_keeps_the_full_sqlite_season_label(self) -> None:
        fixtures = [{**m, 'date_utc': m['date_utc'].replace('2025-', '2026-')} for m in self.canonical]
        path = self.source.with_name('france_ligue_1_2026.json')
        path.write_text(json.dumps({
            'competition': {'league_code': 'F1', 'app_season': '2026-2027', 'api_football_season': '2026'},
            'source': {'provider': 'api-football'}, 'readiness': {'completed_fixtures': 3},
            'matches': fixtures,
        }))
        self.index.write_text(json.dumps({'leagues': [{'league_code': 'F1', 'app_season': '2027-2028'}]}))
        with sqlite3.connect(self.database) as conn:
            for fixture in fixtures:
                self.insert_match(conn, fixture, season='2026-2027')
            self.insert_match(conn, {'date_utc': '2026-09-01', 'home_team': 'Ghost', 'away_team': 'A'}, season='2026-2027')
        self.assertEqual(module.reconcile(self.database, self.index, self.root, 'F1', '2026-2027'), (1, 3))
        self.assertEqual(self.count(), 8)  # Five legacy rows plus the three canonical newer rows.

    def test_source_validation_precedes_the_expensive_builder(self) -> None:
        source = (MODULE_PATH.parents[1] / '.github/workflows/app-ready-artifact-publisher.yml').read_text()
        self.assertLess(source.index('--validate-source-only'), source.index('reactivecircus/android-emulator-runner'))
        self.assertIn('--repo-root . --league-code F1 --app-season 2025-2026', source)

    def test_scope_regression_uses_a_verified_published_bundle_without_actions_artifacts(self) -> None:
        source = (MODULE_PATH.parents[1] / '.github/workflows/app-ready-stats-scope-reconcile-tests.yml').read_text()
        self.assertNotIn('actions/download-artifact', source)
        self.assertIn('scripts/verify_app_ready_stats_reconciliation.py --repo-root . --full-scope', source)
        self.assertIn('--validate-source-only', source)


if __name__ == "__main__":
    unittest.main()
