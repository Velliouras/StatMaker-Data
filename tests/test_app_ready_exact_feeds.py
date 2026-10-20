"""No expensive emulator runs without the exact UEFA price input contract."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from validate_app_ready_exact_odds import check_feeds, COMPETITIONS


class AppReadyExactFeedTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.make_feeds()

    def make_feeds(self):
        for code in COMPETITIONS:
            (self.path / f"{code}_odds.json").write_text(json.dumps({
                "generatedAt": "2026-10-20T00:00:00Z",
                "matches": [{"id": 123, "markets": []}],
            }), encoding="utf-8")

    def test_all_feeds_accepted(self):
        self.assertEqual(check_feeds(self.path), {c: 1 for c in COMPETITIONS})

    def test_missing_feed_fails_before_emulator(self):
        (self.path / "europa_league_odds.json").unlink()
        with self.assertRaisesRegex(ValueError, "Missing UEFA exact feed"):
            check_feeds(self.path)

    def test_corrupt_feed_fails_before_emulator(self):
        (self.path / "conference_league_odds.json").write_text("{bad")
        with self.assertRaises(json.JSONDecodeError):
            check_feeds(self.path)

    def test_missing_markets_fails(self):
        (self.path / "champions_league_odds.json").write_text(json.dumps({
            "generatedAt": "2026-10-20T00:00:00Z",
            "matches": [{"id": 123}],
        }))
        with self.assertRaisesRegex(ValueError, "Bad match"):
            check_feeds(self.path)

    def test_workflow_passes_exact_feed_dir_and_never_uploads_checkpoints(self):
        workflow = (ROOT / ".github/workflows/app-ready-artifact-publisher.yml").read_text()
        self.assertIn("--uefa-exact-feed-dir", workflow)
        self.assertIn("validate_app_ready_exact_odds.py", workflow)
        self.assertLess(workflow.index("Validate exact UEFA odds"),
                        workflow.index("Produce indexed read models off-device"))
        self.assertNotIn("actions/upload-artifact", workflow)

    def test_nightly_is_bounded_to_one_full_run(self):
        workflow = (ROOT / ".github/workflows/app-ready-artifact-publisher.yml").read_text()
        triggers = workflow.partition("\non:\n")[2].partition("\npermissions:")[0]
        self.assertEqual(triggers.count("cron:"), 1)
        self.assertIn("workflow_dispatch:", triggers)
        self.assertNotIn("workflow_run:", triggers)


if __name__ == "__main__":
    unittest.main()
