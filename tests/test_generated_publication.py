"""Exercise real Git repositories, shallow fetches and concurrent main writers."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("generated_publication", ROOT / ".github/scripts/publish_generated_paths.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class GeneratedPublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.author = self.root / "author"
        self.worker = self.root / "worker"
        self.command(self.root, "init", "--bare", "-q", "--initial-branch=main", str(self.remote))
        self.command(self.root, "clone", "-q", str(self.remote), str(self.author))
        self.identity(self.author)
        self.commit(self.author, "foreign.json", "initial", "Initial input")
        self.command(self.author, "push", "-q", "origin", "main")
        # This upstream commit must never be replayed as a local publisher commit.
        self.commit(self.author, "foreign.json", "priority-results", "Priority result publication")
        self.command(self.author, "push", "-q", "origin", "main")
        self.command(self.root, "clone", "-q", "--depth=1", self.remote.as_uri(), str(self.worker))
        self.identity(self.worker)
        self.commit(self.worker, "owned.json", "new-scorers", "Own generated publication")
        self.previous = Path.cwd()
        os.chdir(self.worker)
        self.addCleanup(os.chdir, self.previous)

    def command(self, cwd, *args, check=True):
        return subprocess.run(["git", *args], cwd=cwd, check=check, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def identity(self, repo):
        self.command(repo, "config", "user.name", "Regression Test")
        self.command(repo, "config", "user.email", "regression@example.invalid")

    def commit(self, repo, path, value, message):
        (repo / path).write_text(value)
        self.command(repo, "add", path)
        self.command(repo, "commit", "-qm", message)

    def advance(self, count=1):
        for n in range(count):
            self.commit(self.author, "foreign.json", f"newer-result-{n}", f"Other writer {n}")
        self.command(self.author, "push", "-q", "origin", "main")

    def remote_file(self, path):
        return self.command(self.remote, "show", f"main:{path}").stdout

    def test_shallow_checkout_replays_only_own_commit_and_preserves_newer_results(self):
        self.advance(5)
        publisher.publish(prefixes=("owned.json",), pause=lambda _: None)
        self.assertEqual("new-scorers", self.remote_file("owned.json"))
        self.assertEqual("newer-result-4", self.remote_file("foreign.json"))
        messages = self.command(self.remote, "log", "--format=%s", "main").stdout.splitlines()
        self.assertEqual(1, messages.count("Priority result publication"))
        self.assertEqual(1, messages.count("Own generated publication"))

    def test_previous_depth_two_fetch_does_not_replay_unrelated_old_commit(self):
        self.advance(6)
        self.command(self.worker, "fetch", "--depth=2", "origin", "main")
        publisher.publish(prefixes=("owned.json",), pause=lambda _: None)
        self.assertEqual("newer-result-5", self.remote_file("foreign.json"))
        self.assertEqual("new-scorers", self.remote_file("owned.json"))

    def test_transient_push_rejection_is_retried_without_regenerating_outputs(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text('#!/bin/sh\nif [ ! -e "retry-marker" ]; then touch "retry-marker"; exit 1; fi\nexit 0\n')
        hook.chmod(0o755)
        publisher.publish(prefixes=("owned.json",), pause=lambda _: None)
        self.assertEqual("new-scorers", self.remote_file("owned.json"))

    def test_same_path_conflict_never_overwrites_newer_canonical_data(self):
        self.commit(self.author, "owned.json", "newer-canonical", "Another canonical owner")
        self.command(self.author, "push", "-q", "origin", "main")
        with self.assertRaisesRegex(RuntimeError, "conflicts with newer canonical"):
            publisher.publish(prefixes=("owned.json",), pause=lambda _: None)
        self.assertEqual("newer-canonical", self.remote_file("owned.json"))

    def test_ownership_check_rejects_unrelated_files_before_publication(self):
        with self.assertRaisesRegex(RuntimeError, "outside the publisher"):
            publisher.publish(prefixes=("other-output/",), pause=lambda _: None)
        self.assertNotEqual(self.command(self.remote, "cat-file", "-e", "main:owned.json", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
