import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / '.github/scripts/publish_postmatch_stats.py'
spec = importlib.util.spec_from_file_location('publisher', MODULE)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def run(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.work = self.root / 'work'
        self.other = self.root / 'other'
        run(self.root, 'init', '--bare', str(self.remote))
        run(self.root, 'clone', str(self.remote), str(self.work))
        self.setup_repo(self.work)
        run(self.work, 'checkout', '-b', 'main')
        self.write(self.work, 'data/api_football/fixture_stats/a.json', 'old\n')
        self.write(self.work, 'data/statmaker/domestic_enriched/a.json', 'old\n')
        self.write(self.work, 'unrelated.txt', 'old\n')
        run(self.work, 'add', '.')
        run(self.work, 'commit', '-m', 'base')
        run(self.work, 'push', 'origin', 'main')
        run(self.root, 'clone', '-b', 'main', str(self.remote), str(self.other))
        self.setup_repo(self.other)
        self.write(self.work, 'data/api_football/fixture_stats/a.json', 'verified\n')
        run(self.work, 'add', '.')
        run(self.work, 'commit', '-m', 'source')
        self.cwd = Path.cwd()
        os.chdir(self.work)

    def tearDown(self):
        os.chdir(self.cwd)
        self.temp.cleanup()

    def setup_repo(self, root):
        run(root, 'config', 'user.name', 'Test')
        run(root, 'config', 'user.email', 'test@example.invalid')

    def write(self, root, path, text):
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def competing_commit(self, path, value):
        self.write(self.other, path, value)
        run(self.other, 'add', '.')
        run(self.other, 'commit', '-m', 'concurrent writer')
        run(self.other, 'push', 'origin', 'main')

    def test_rebuild_after_push_race_preserves_concurrent_data(self):
        calls = []
        def builder():
            calls.append(1)
            latest = Path('unrelated.txt').read_text()
            self.write(self.work, 'data/statmaker/domestic_enriched/a.json', 'derived:' + latest)
            if len(calls) == 1:
                self.competing_commit('unrelated.txt', 'latest\n')
        publisher.publish(True, builder=builder, pause=lambda _: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(Path('data/statmaker/domestic_enriched/a.json').read_text(), 'derived:latest\n')
        self.assertEqual(Path('data/api_football/fixture_stats/a.json').read_text(), 'verified\n')
        self.assertEqual(run(self.work, 'rev-parse', 'HEAD'), run(self.work, 'rev-parse', 'origin/main'))

    def test_existing_derived_change_is_rebuilt(self):
        self.competing_commit('data/statmaker/domestic_enriched/a.json', 'newer derived\n')
        def builder():
            self.assertEqual(Path('data/statmaker/domestic_enriched/a.json').read_text(), 'newer derived\n')
            self.write(self.work, 'data/statmaker/domestic_enriched/a.json', 'combined\n')
        publisher.publish(True, builder=builder)
        self.assertEqual(Path('data/statmaker/domestic_enriched/a.json').read_text(), 'combined\n')

    def test_source_conflict_refuses_to_overwrite(self):
        self.competing_commit('data/api_football/fixture_stats/a.json', 'newer provider\n')
        before = run(self.other, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(RuntimeError, 'Provider cache conflict'):
            publisher.publish(True, builder=lambda: self.fail('must not rebuild conflict'))
        self.assertEqual(before, run(self.other, 'ls-remote', 'origin', 'refs/heads/main').split()[0] + '\n')

    def test_retry_state_only_does_not_rebuild(self):
        publisher.publish(False, builder=lambda: self.fail('state-only must not rebuild'))
        self.assertEqual(Path('data/statmaker/domestic_enriched/a.json').read_text(), 'old\n')

    def test_rebuild_targets_canonical_manifest(self):
        with patch.object(publisher.subprocess, 'run') as invoke:
            publisher.rebuild()
        commands = [call.args[0] for call in invoke.call_args_list]
        self.assertEqual(commands[-1][-2:], ['--output', 'data/statmaker/update_manifest.json'])
        self.assertTrue(all('refresh_recent_finished_match_stats.py' not in ' '.join(c) for c in commands))


if __name__ == '__main__':
    unittest.main()
