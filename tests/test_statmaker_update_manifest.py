import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_statmaker_update_manifest import ArtifactSpec, build_manifest, write_manifest


class StatMakerUpdateManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.specs = (
            ArtifactSpec("domestic_odds", "odds/domestic.json", "odds", required=True),
            ArtifactSpec("support", "data/support.json", "support"),
        )
        (self.root / "odds").mkdir(parents=True)
        (self.root / "odds/domestic.json").write_text(
            json.dumps({"generatedAt": "2026-07-27T10:00:00Z", "matches": [1]}),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_manifest_is_deterministic_and_content_addressed(self):
        first = build_manifest(self.root, "main", specs=self.specs, profile="main")
        second = build_manifest(self.root, "main", specs=self.specs, profile="main")
        self.assertEqual(first, second)
        self.assertEqual(first["schemaVersion"], 2)
        self.assertEqual(first["profile"], "main")
        self.assertEqual(first["artifactCount"], 1)
        self.assertEqual(first["generatedAt"], "2026-07-27T10:00:00Z")

        output = self.root / "data/manifest.json"
        self.assertTrue(write_manifest(output, first))
        self.assertFalse(write_manifest(output, second))

        (self.root / "odds/domestic.json").write_text(
            json.dumps({"generatedAt": "2026-07-27T11:00:00Z", "matches": [1, 2]}),
            encoding="utf-8",
        )
        changed = build_manifest(self.root, "main", specs=self.specs, profile="main")
        self.assertNotEqual(first["contentVersion"], changed["contentVersion"])
        self.assertEqual(changed["generatedAt"], "2026-07-27T11:00:00Z")

    def test_branch_is_reflected_in_artifact_url(self):
        manifest = build_manifest(self.root, "build/live", specs=self.specs, profile="uefa")
        self.assertEqual(manifest["profile"], "uefa")
        self.assertIn("/build/live/odds/domestic.json", manifest["artifacts"][0]["url"])

    def test_missing_optional_artifact_is_omitted(self):
        manifest = build_manifest(self.root, "main", specs=self.specs)
        self.assertEqual([item["id"] for item in manifest["artifacts"]], ["domestic_odds"])

    def test_missing_required_artifact_fails(self):
        (self.root / "odds/domestic.json").unlink()
        with self.assertRaises(FileNotFoundError):
            build_manifest(self.root, "main", specs=self.specs)


if __name__ == "__main__":
    unittest.main()
