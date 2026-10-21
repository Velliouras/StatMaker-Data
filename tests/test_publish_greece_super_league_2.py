"""Real git remotes reproduce stale-manifest rebases and a rejected push race."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "data/api_football/fixture_stats/greece/super-league-2/2025/fixture_stats.json",
    "data/statmaker/domestic_enriched/greece_super_league_2_2025.json",
    "data/statmaker/domestic_enriched/index.json",
    "odds/odds_api_io/domestic_odds.json",
    "reports/greece_super_league_2_sync.json",
    "reports/domestic_odds_integrity.json",
)
MANIFEST = "data/statmaker/update_manifest.json"
OTHER = "data/statmaker/uefa_current_stats.json"


class PublishG2Test(unittest.TestCase):
    def run_cmd(self, cwd, *cmd, check=True):
        return subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=check, timeout=40)

    def git(self, cwd, *args):
        return self.run_cmd(cwd, "git", *args).stdout.strip()

    def write(self, repo, path, value):
        out = repo / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"generatedAt": "2026-10-21T07:00:00Z", "value": value}))

    def manifest(self, repo):
        self.run_cmd(repo, sys.executable, "scripts/build_statmaker_update_manifest.py",
                     "--profile", "main", "--branch", "main", "--output", MANIFEST)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.remote, self.work, self.other = (base / x for x in ("remote.git", "work", "other"))
        self.git(base, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(base, "clone", str(self.remote), str(self.work))
        self.git(self.work, "config", "user.name", "Test")
        self.git(self.work, "config", "user.email", "test@example.invalid")
        (self.work / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/build_statmaker_update_manifest.py", self.work / "scripts")
        for path in FILES + (OTHER,):
            self.write(self.work, path, "initial")
        self.manifest(self.work)
        self.git(self.work, "add", ".")
        self.git(self.work, "commit", "-m", "initial")
        self.git(self.work, "push", "origin", "HEAD:main")
        self.git(base, "clone", str(self.remote), str(self.other))
        self.git(self.other, "config", "user.name", "Other producer")
        self.git(self.other, "config", "user.email", "other@example.invalid")
        for path in FILES:
            self.write(self.work, path, "g2-new")

    def advance_other(self, path=OTHER):
        self.write(self.other, path, "concurrent-new")
        self.manifest(self.other)
        self.git(self.other, "add", ".")
        self.git(self.other, "commit", "-m", "concurrent producer and manifest")
        self.git(self.other, "push", "origin", "HEAD:main")

    def publish(self):
        return self.run_cmd(self.work, "bash", str(ROOT / "scripts/publish_greece_super_league_2.sh"),
                            check=False)

    def test_stale_manifest_is_rebuilt_without_losing_other_producer(self):
        self.advance_other()
        result = self.publish()
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertEqual("g2-new", json.loads(self.git(
            self.work, "show", "origin/main:" + FILES[1]))["value"])
        self.assertEqual("concurrent-new", json.loads(self.git(
            self.work, "show", "origin/main:" + OTHER))["value"])
        actual = (self.work / MANIFEST).read_text()
        self.manifest(self.work)
        self.assertEqual(actual, (self.work / MANIFEST).read_text())

    def test_real_source_overlap_is_rejected_without_overwrite(self):
        self.advance_other(FILES[2])
        remote_before = self.git(self.other, "rev-parse", "HEAD")
        result = self.publish()
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("refusing to overwrite", result.stdout)
        self.assertEqual(remote_before, self.git(self.work, "ls-remote", "origin", "refs/heads/main").split()[0])

    def test_remote_push_race_retries_without_replaying_manifest(self):
        hook = self.work / ".git/hooks/pre-push"
        # Only the first push moves the remote. The next attempt must recombine
        # the saved provider output with that new tree and regenerate metadata.
        code = "\n".join([
            "from pathlib import Path", "import os, subprocess, sys",
            "marker=Path(" + repr(str(self.work / ".git/race-done")) + ")",
            "if marker.exists(): sys.exit(0)", "marker.touch()",
            "env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}",
            "repo=" + repr(str(self.other)),
            "p=Path(repo)/" + repr(OTHER),
            "p.write_text('{\"value\":\"race-new\"}')",
            "def run(*args): subprocess.run(args,cwd=repo,env=env,check=True)",
            "run(sys.executable,'scripts/build_statmaker_update_manifest.py','--profile','main','--branch','main','--output'," + repr(MANIFEST) + ")",
            "run('git','add','.')", "run('git','commit','-m','race')",
            "run('git','push','origin','HEAD:main')", "",
        ])
        hook.write_text("#!" + sys.executable + "\n" + code)
        hook.chmod(0o755)
        result = self.publish()
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("G2_PUBLISH_OK attempt=2", result.stdout)
        self.assertEqual("race-new", json.loads(self.git(
            self.work, "show", "origin/main:" + OTHER))["value"])


if __name__ == "__main__":
    unittest.main()
