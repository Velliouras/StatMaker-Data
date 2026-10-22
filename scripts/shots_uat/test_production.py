import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from .publish_production import SOURCE, TARGET, publish


class ProductionPublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.row = dict(selectionId="first-issue", odd=1.95,
                        publicationCommit="a" * 40, publishedAt="2026-10-22T08:00:00+00:00",
                        kickoff="2026-10-22T20:00:00+00:00")
        self.payload = dict(schemaVersion=1, status="UAT_SHOTS_MODEL", apiCalls=0, prodWrites=0,
                            generationId="frozen-generation", generatedAt="2026-10-22T09:00:00+00:00",
                            issued=[self.row], candidates=[self.row], candidateCount=1)

    def stage(self):
        source = self.root / SOURCE
        source.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(self.payload, sort_keys=True) + "\n").encode()
        sha = hashlib.sha256(content).hexdigest()
        index = dict(schemaVersion=1, channel="uat", artifact=f"candidates-{sha}.json", sha256=sha,
                     generationId=self.payload["generationId"], generatedAt=self.payload["generatedAt"])
        (source / index["artifact"]).write_bytes(content)
        (source / "latest.json").write_text(json.dumps(index))
        return source, index, content

    def test_production_uses_identical_issues_prices_and_bytes_and_leaves_source_unchanged(self):
        source, index, content = self.stage()
        old_index = (source / "latest.json").read_bytes()
        result = publish(self.root)
        self.assertEqual("prod", result["channel"])
        self.assertEqual(index["sha256"], result["sha256"])
        self.assertEqual(content, (self.root / TARGET / index["artifact"]).read_bytes())
        self.assertEqual(old_index, (source / "latest.json").read_bytes())
        self.assertEqual(result, publish(self.root))

    def test_rejects_bad_sha_before_exposing_a_production_index(self):
        source, index, _ = self.stage()
        (source / index["artifact"]).write_text("changed")
        with self.assertRaisesRegex(ValueError, "SHA"):
            publish(self.root)
        self.assertFalse((self.root / TARGET / "latest.json").exists())

    def test_rejects_unissued_candidate(self):
        self.payload["issued"] = []
        self.stage()
        with self.assertRaisesRegex(ValueError, "published issues"):
            publish(self.root)

    def test_rejects_post_kickoff_issue(self):
        self.row["publishedAt"] = "2026-10-22T21:00:00+00:00"
        self.stage()
        with self.assertRaisesRegex(ValueError, "pre-kickoff"):
            publish(self.root)

    def test_rejects_mixed_generation(self):
        source, index, _ = self.stage()
        index["generationId"] = "different-generation"
        (source / "latest.json").write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, "generation"):
            publish(self.root)

    def test_preserves_market_issues_in_the_same_generation(self):
        market = dict(self.row, selectionId="other-market", odd=2.10)
        self.payload.update(marketIssued=[market], marketCandidates=[market], marketCandidateCount=1)
        _, index, content = self.stage()
        publish(self.root)
        self.assertEqual(content, (self.root / TARGET / index["artifact"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
