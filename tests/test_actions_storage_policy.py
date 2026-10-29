"""Prevent storage regressions without requiring a runner or provider requests."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('storage_policy', Path(__file__).resolve().parents[1] / '.github/scripts/check_no_artifacts.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class ActionsStoragePolicyTest(unittest.TestCase):
    def check(self, steps):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'test.yml').write_text('jobs:\n  build:\n    steps:\n' + steps)
            return policy.violations(directory)

    def test_artifact_uploads_fail(self):
        for action in ('upload-artifact', 'upload-pages-artifact'):
            self.assertEqual(1, len(self.check(f'      - uses: actions/{action}@v4\n')))

    def test_gradle_cache_must_be_explicitly_disabled(self):
        for extra in ('', '        with:\n          cache-disabled: false\n'):
            self.assertEqual(1, len(self.check('      - uses: gradle/actions/setup-gradle@v4\n' + extra)))

    def test_disabled_gradle_cache_passes(self):
        self.assertEqual([], self.check('      - name: Setup Gradle\n        uses: gradle/actions/setup-gradle@v4\n        with:\n          cache-disabled: true\n'))

    def test_later_disabled_step_does_not_excuse_earlier_cache(self):
        self.assertEqual(1, len(self.check('      - uses: gradle/actions/setup-gradle@v4\n      - uses: gradle/actions/setup-gradle@v4\n        with:\n          cache-disabled: true\n')))

    def test_next_job_cannot_disable_previous_step_cache(self):
        self.assertEqual(1, len(self.check('      - uses: gradle/actions/setup-gradle@v4\n  other:\n    cache-disabled: true\n')))


if __name__ == '__main__':
    unittest.main()
