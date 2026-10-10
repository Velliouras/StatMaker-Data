"""End-to-end completely offline JSON xG import and temporal validator integration."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from recover_understat_xg import main as import_main
from validate_xg_overlay import inspect_overlay


class UnderstatOfflineImportPipelineTests(unittest.TestCase):
    def build_fixture(self, root: Path):
        enriched_path = root / "data/statmaker/domestic_enriched/epl2026.json"
        index_path = root / "data/statmaker/domestic_enriched/index.json"
        source_path = root / "understat_cache.json"
        enriched_path.parent.mkdir(parents=True)
        canonical = {
            "competition": {"league_code": "E0"}, "matches": [{
                "fixture_id": 777, "status": "FT",
                "date_utc": "2026-09-05T18:00:00+00:00",
                "home_team": "Arsenal", "away_team": "Coventry",
                "home_goals": 3, "away_goals": 1,
                "normalized_stats": {"HxG": None, "AxG": None}
            }]
        }
        index_path.write_text(json.dumps({"schema_version": 3, "leagues": [{
            "league_code": "E0", "api_football_season": "2026",
            "output_path": "data/statmaker/domestic_enriched/epl2026.json"
        }]}), encoding="utf-8")
        enriched_path.write_text(json.dumps(canonical), encoding="utf-8")
        source_path.write_text(json.dumps({"dates": [{
            "id": "31180", "isResult": True,
            "h": {"title": "Arsenal"}, "a": {"title": "Coventry"},
            "goals": {"h": "3", "a": "1"},
            "xG": {"h": "1.85", "a": "0.56"},
            "datetime": "2026-09-05 18:00:00"
        }]}), encoding="utf-8")
        return source_path, canonical

    def test_local_json_import_then_strict_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, canonical = self.build_fixture(root)
            output = root / "reports/betting_v2/understat_xg_E0_2026.json"
            args = [
                "recover_understat_xg.py", "--repository-root", str(root),
                "--league", "E0", "--season", "2026", "--input", str(source),
                "--source-commit", "a"*40, "--source-blob-sha", "b"*40,
                "--source-commit-at", "2026-10-06T20:59:42Z",
                "--output", str(output),
            ]
            with patch.object(sys, "argv", args):
                import_main()
            doc = json.loads(output.read_text())
            self.assertEqual(doc["retrievalMode"], "LOCAL_UNDERSTAT_JSON_NO_NETWORK")
            self.assertFalse(doc["mayCertifyPicks"])
            self.assertEqual(len(doc["recovered"]), 1)
            report = inspect_overlay(doc, canonical)
            self.assertEqual(report["validResearchOverlayRows"], 1)
            self.assertTrue(report["allRowsPassed"])
            self.assertFalse(report["canPublishStrong"])
            self.assertFalse(report["readyForHistoricalBacktestBeforeSourceCommit"])

    def test_missing_provenance_stays_uncertified_and_validator_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, canonical = self.build_fixture(root)
            output = root / "reports/betting_v2/unverified.json"
            with patch.object(sys, "argv", [
                "recover_understat_xg.py", "--repository-root", str(root),
                "--league", "E0", "--season", "2026",
                "--input", str(source), "--output", str(output)
            ]):
                import_main()
            doc = json.loads(output.read_text())
            self.assertIsNone(doc["sourceCommit"])
            with self.assertRaises(ValueError):
                inspect_overlay(doc, canonical)

    def test_partial_git_source_provenance_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _ = self.build_fixture(root)
            with patch.object(sys, "argv", [
                "recover_understat_xg.py", "--repository-root", str(root),
                "--league", "E0", "--season", "2026",
                "--input", str(source), "--source-commit", "a"*40
            ]):
                with self.assertRaises(ValueError):
                    import_main()


if __name__ == "__main__":
    unittest.main()
