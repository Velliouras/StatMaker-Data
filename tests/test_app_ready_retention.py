"""Keep installed clients' bundles while pruning old unreferenced App-Ready files."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prune_app_ready_unreferenced_bundles import prune


def bundle(prefix: str, digit: str) -> str:
    return f"app_ready_{prefix}_bundle-{digit * 64}.zip"


def manifest(folder, stats, betting):
    return {"schemaVersion": 1, "artifacts": [
        {"id": "app_ready_stats_bundle",
         "path": f"data/statmaker/app_ready/{stats}"},
        {"id": "app_ready_betting_bundle",
         "path": f"data/statmaker/app_ready/{betting}"}
    ]}


class AppReadyRetentionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "app_ready"
        self.folder.mkdir()
        self.old = Path(tmp.name) / "previous.json"
        self.stats = bundle("stats", "a")
        self.betting = bundle("betting", "b")
        self.prev_stats = bundle("stats", "c")
        self.prev_betting = bundle("betting", "d")
        self.unref = bundle("betting", "e")
        for name in (self.stats, self.betting, self.prev_stats,
                     self.prev_betting, self.unref):
            (self.folder / name).write_bytes(b"DATA")
        (self.folder / "update_manifest.json").write_text(
            json.dumps(manifest(self.folder, self.stats, self.betting)))
        self.old.write_text(json.dumps(
            manifest(self.folder, self.prev_stats, self.prev_betting)))
        (self.folder / "publish_status.json").write_text('{"status":"READY"}')
        (self.folder / "other.zip").write_bytes(b"other")

    def test_only_unreferenced_bundle_is_pruned(self):
        summary = prune(self.folder, self.old)
        self.assertEqual(summary["removedBundles"], 1)
        for f in (self.stats, self.betting, self.prev_stats, self.prev_betting,
                  "publish_status.json", "other.zip"):
            self.assertTrue((self.folder / f).is_file())
        self.assertFalse((self.folder / self.unref).exists())

    def test_repeated_pruning_is_noop(self):
        prune(self.folder, self.old)
        self.assertEqual(prune(self.folder, self.old)["removedBundles"], 0)

    def test_missing_active_bundle_refuses_deletion(self):
        (self.folder / self.stats).unlink()
        with self.assertRaisesRegex(ValueError, "Active App-Ready bundle missing"):
            prune(self.folder, self.old)
        self.assertTrue((self.folder / self.unref).is_file())

    def test_malformed_active_manifest_refuses_deletion(self):
        (self.folder / "update_manifest.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Malformed"):
            prune(self.folder, self.old)
        self.assertTrue((self.folder / self.unref).is_file())


if __name__ == "__main__":
    unittest.main()
