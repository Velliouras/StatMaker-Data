"""Offline-only V2 shadow publisher safety regression tests.

These tests must be run deliberately; never through an Android build or new
scheduled GitHub Action. They use no provider API and only temp files.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts" / "betting_v2"
sys.path.insert(0, str(SCRIPT_DIR))
from publish_shadow import PROVIDER_REQUEST_BUDGET, _safe_output, publish


class BettingV2ZeroQuotaShadowTests(unittest.TestCase):
    def test_offline_modules_import_no_network_clients(self):
        prohibited = {"requests", "httpx", "urllib", "aiohttp", "socket",
                      "http.client", "ftplib", "smtplib"}
        for path in SCRIPT_DIR.glob("*.py"):
            with self.subTest(script=path.name):
                syntax = ast.parse(path.read_text(encoding="utf-8"))
                imports = set()
                for node in ast.walk(syntax):
                    if isinstance(node, ast.Import):
                        imports.update(item.name for item in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.add(node.module)
                self.assertFalse(any(
                    imported == banned or imported.startswith(banned + ".")
                    for imported in imports for banned in prohibited
                ), f"{path.name} imported a network client")

    def test_budget_is_hard_zero(self):
        self.assertEqual(PROVIDER_REQUEST_BUDGET, 0)

    def test_output_is_bounded_to_shadow_reports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(
                _safe_output(root, Path("reports/betting_v2/manifest.json")),
                root / "reports/betting_v2/manifest.json"
            )
            for destination in (
                "data/statmaker/app_ready/update_manifest.json",
                "data/statmaker/update_manifest.json",
                "odds/odds_api_io/domestic_odds.json",
                ".github/workflows/new-action.yml",
                "reports/other/path.json",
            ):
                with self.subTest(destination=destination):
                    with self.assertRaises(ValueError):
                        _safe_output(root, Path(destination))

    def test_shadow_cannot_issue_certified_forecasts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "data/statmaker/domestic_enriched/index.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({
                "schema_version": 3, "leagues": []
            }), encoding="utf-8")
            result = publish(
                root, Path("reports/betting_v2/shadow_manifest.json"))
            self.assertEqual(result["liveRecommendationsPublished"], 0)
            self.assertEqual(result["certifiedForecasts"], [])
            self.assertEqual(result["certificationStatus"], "BLOCKED")
            self.assertEqual(result["api"]["callsMadeByThisPublisher"], 0)
            self.assertFalse((root / "data/statmaker/app_ready").exists())


if __name__ == "__main__":
    unittest.main()
