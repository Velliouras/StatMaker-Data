import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import materialize_prepared_fixture_index as index
import audit_app_ready_fixture_index_uniqueness as audit


def fixture(key, home="Chelsea", away="Tottenham", day="2026-10-24", league="E0"):
    return ("domestic", "snapshot", key, day, key, day, day, league,
            "England", "Premier League", "2026-2027", home, away, home, away,
            home, away, None, None, "schedule_only", 1, None)


class ScheduleGapsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        index.create_schema(self.db)
        self.db.execute("INSERT INTO prepared_fixture_matches VALUES(" +
                        ",".join("?" * 22) + ")", fixture("odds-1"))

    def tearDown(self):
        self.db.close()

    def select(self, rows):
        with contextlib.redirect_stdout(io.StringIO()):
            return index.supplemental_rows_for_gaps(self.db, rows)

    def test_other_provider_id_does_not_duplicate_catalog_fixture(self):
        self.assertEqual([], self.select([fixture("api-1")]))
        self.assertEqual("odds-1", self.db.execute(
            "SELECT match_key FROM prepared_fixture_matches").fetchone()[0])

    def test_conflicting_opponent_cannot_occupy_same_team_date(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual([], index.supplemental_rows_for_gaps(
                self.db, [fixture("api-2", away="Arsenal")]))
        self.assertIn("CATALOG_ALREADY_OCCUPIES_TEAM_DATE", out.getvalue())

    def test_real_gaps_other_dates_and_leagues_remain(self):
        rows = [fixture("api-2", "Everton", "Fulham"),
                fixture("api-3", day="2026-10-25"),
                fixture("api-4", league="OTHER")]
        self.assertEqual(rows, self.select(rows))

    def test_canonical_club_decoration_does_not_duplicate_fixture(self):
        self.assertEqual([], self.select([fixture("api-1", home="Chelsea FC")]))

    def test_conflicting_supplemental_cache_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Conflicting supplemental"):
            self.select([fixture("api-2", "Everton", "Fulham"),
                         fixture("api-3", "Everton", "Arsenal")])

    def test_audit_selects_manifest_current_not_retained_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "app_ready_betting_bundle-current.zip"
            current.touch()
            (root / "app_ready_betting_bundle-retained.zip").touch()
            (root / "update_manifest.json").write_text(json.dumps({"artifacts": [{
                "id": "app_ready_betting_bundle",
                "path": "data/statmaker/app_ready/" + current.name}]}))
            with patch.object(audit.settlement, "APP_READY_DIR", root):
                self.assertEqual([current], audit.current_fixture_bundle_paths())
                current.unlink()
                with self.assertRaisesRegex(SystemExit, "CURRENT_BUNDLE_MISSING"):
                    audit.current_fixture_bundle_paths()

    def test_materialize_keeps_catalog_markets_and_fills_only_missing_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prepared.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE prepared_snapshot_meta (competition_id TEXT, "
                       "snapshot_version TEXT, catalog_payload TEXT, state TEXT)")
            for competition in index.COMPETITIONS:
                db.execute("INSERT INTO prepared_snapshot_meta VALUES (?,?,?,?)", (
                    competition, "snapshot", json.dumps({"matches": [{
                        "id": "odds-1", "date": "2026-10-24", "leagueCode": "E0",
                        "homeTeam": "Chelsea", "awayTeam": "Tottenham",
                        "markets": [{"market": "TOTAL_GOALS", "selection": "Over",
                                     "line": 2.5, "odd": 1.9}]}]}), "ready"))
            db.commit()
            db.close()
            with patch.object(index, "fixture_dispositions", return_value=[]), \
                 patch.object(index, "supplemental_domestic_schedule_rows", return_value=[
                     fixture("api-1"), fixture("api-2", "Everton", "Fulham")]), \
                 contextlib.redirect_stdout(io.StringIO()):
                index.materialize(path)
            db = sqlite3.connect(path)
            self.assertEqual([("api-2",), ("odds-1",)], db.execute(
                "SELECT match_key FROM prepared_fixture_matches "
                "WHERE competition_id='domestic' ORDER BY match_key").fetchall())
            self.assertEqual(4, db.execute("SELECT COUNT(*) FROM prepared_fixture_markets").fetchone()[0])
            self.assertEqual(1.9, db.execute("SELECT odd FROM prepared_fixture_markets LIMIT 1").fetchone()[0])
            db.close()


class CatalogProviderIdentityTest(unittest.TestCase):
    def match(self, key, kickoff="2026-10-31T13:00:00Z", league="T1", away="Rizespor", odd=1.91):
        return dict(id=key, kickoff=kickoff, date=kickoff[:10], leagueCode=league,
            homeTeam="Kocaelispor", awayTeam=away, canonicalHomeTeam="Kocaelispor", canonicalAwayTeam=away,
            markets=[dict(market="1X2", selection="Home", team=None, line=None, odd=odd)])

    def collect(self, matches, preferred=frozenset()):
        with contextlib.redirect_stdout(io.StringIO()):
            return index.all_catalog_matches({"matches": matches, "leagues": [{"matches": matches}]}, preferred)

    def test_two_provider_ids_for_exact_same_game_become_one_fixture_with_all_markets(self):
        api = self.match("1584454", kickoff="2026-10-31T13:00:00+00:00")
        odds = self.match("72723300")
        odds["markets"].append(dict(market="BTTS", selection="Yes", team=None, line=None, odd=1.85))
        result = self.collect([api, odds])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "72723300")
        self.assertEqual(result[0]["markets"], odds["markets"])
        self.assertEqual(len(api["markets"]), 1)  # inputs/receipts are not rewritten

    def test_existing_recommendation_key_and_its_price_are_preserved(self):
        api = self.match("1584454", odd=1.93)
        odds = self.match("72723300", odd=1.95)
        odds["markets"].append(dict(market="BTTS", selection="Yes", odd=1.85))
        result = self.collect([api, odds], {"1584454"})
        self.assertEqual(result[0]["id"], "1584454")
        self.assertEqual(result[0]["markets"][0]["odd"], 1.93)
        self.assertEqual(len(result[0]["markets"]), 2)

    def test_different_opponents_kickoffs_or_leagues_are_never_merged(self):
        api = self.match("1584454")
        for other in (self.match("other", away="Trabzonspor"),
                      self.match("other", kickoff="2026-10-31T16:00:00Z"),
                      self.match("other", league="OTHER")):
            self.assertEqual(len(self.collect([api, other])), 2)

    def test_unknown_kickoff_timezone_is_not_guessed(self):
        self.assertEqual(len(self.collect([self.match("api", kickoff="2026-10-31T13:00:00"),
                                           self.match("odds")])), 2)

    def test_multiple_referenced_provider_ids_remain_unchanged_in_source_records(self):
        api, odds = self.match("api", odd=1.93), self.match("odds", odd=1.95)
        result = self.collect([api, odds], {"api", "odds"})
        self.assertEqual(len(result), 1)
        self.assertEqual((api["id"], api["markets"][0]["odd"]), ("api", 1.93))
        self.assertEqual((odds["id"], odds["markets"][0]["odd"]), ("odds", 1.95))


if __name__ == "__main__":
    unittest.main()
