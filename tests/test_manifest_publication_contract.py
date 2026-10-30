"""Keep shared manifest publication atomic across the affected producer workflows."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / '.github/workflows'
WORKFLOWS = (
    'model-performance-settlement-refresh.yml', 'domestic-lineup-watch.yml',
    'domestic-match-context.yml', 'domestic-squad-context.yml',
    'turkey-super-lig-schedule.yml', 'uefa-support-backfill.yml',
    'domestic-historical-closure.yml',
)


class ManifestPublicationContractTest(unittest.TestCase):
    def test_producers_rebuild_shared_manifest_inside_each_bounded_push_attempt(self):
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                source = (ROOT / name).read_text()
                self.assertIn('python .github/scripts/publish_generated_paths.py --rebuild-main-manifest', source)
                self.assertIn('--path-prefix data/statmaker/update_manifest.json', source)
                self.assertIn('group: statmaker-canonical-main-writer', source)
                self.assertNotIn('git pull --rebase', source)
                self.assertNotIn('deferring', source)
                self.assertNotIn('continue-on-error:', source)

    def test_unpublished_producer_scratch_cannot_influence_canonical_manifest_hashes(self):
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                source = (ROOT / name).read_text()
                commit = source.rindex('git commit -m')
                stash = source.rindex('git stash push --include-untracked')
                publish = source.rindex('python .github/scripts/publish_generated_paths.py')
                self.assertLess(commit, stash)
                self.assertLess(stash, publish)

    def test_live_publisher_retries_its_owned_results_without_overwriting_other_writers(self):
        source = (ROOT / 'live-settlement-publisher.yml').read_text()
        self.assertIn('python .github/scripts/publish_generated_paths.py', source)
        self.assertNotIn('git rebase -X theirs', source)
        for path in ('data/statmaker/live_settlements.json', 'data/api_football/live_settlement_cache.json',
                     'data/statmaker/fixture_validity.json'):
            self.assertIn('--path-prefix ' + path, source)


if __name__ == '__main__':
    unittest.main()
