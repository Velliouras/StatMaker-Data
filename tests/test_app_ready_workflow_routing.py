"""Prevent regression to automatic 90-minute App-Ready rebuilds and racing hot publishers."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
HEAVY = "app-ready-artifact-publisher.yml"
TARGETED = (
    "app-ready-stats-hot-publish.yml",
    "app-ready-simulation-hot-publish.yml",
    "app-ready-score-edge-hot-publish.yml",
)


def workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def block(source: str, heading: str, end: str) -> str:
    match = re.search(r"(?m)^" + re.escape(heading) + r":\s*$", source)
    if not match:
        raise AssertionError(f"Missing {heading!r}")
    finish = re.search(r"(?m)^" + re.escape(end) + r":\s*$", source[match.end():])
    if not finish:
        raise AssertionError(f"Missing {end!r} after {heading!r}")
    return source[match.end():match.end() + finish.start()]


class AppReadyRoutingContractTest(unittest.TestCase):
    def test_heavy_publisher_only_explicit(self) -> None:
        source = workflow(HEAVY)
        on = block(source, "on", "permissions")
        self.assertIn("workflow_dispatch:", on)
        self.assertIn("'.github/app-ready-rebuild-trigger'", on)
        self.assertNotIn("workflow_run:", on)
        self.assertNotRegex(on, r"(?m)^  schedule:")
        self.assertNotIn("data/statmaker/update_manifest.json", on)
        self.assertNotIn("odds/odds_api_io/domestic_odds.json", on)
        self.assertIn("APP_READY_PREPARED_MAX_SECONDS: \"7200\"", source)
        self.assertIn("timeout-minutes: 150", source)

    def test_targeted_publishers_share_non_cancelling_queue(self) -> None:
        for file in TARGETED:
            with self.subTest(file=file):
                source = workflow(file)
                concurrency = block(source, "concurrency", "jobs")
                self.assertIn("group: statmaker-app-ready-targeted-writers", concurrency)
                self.assertIn("queue: max", concurrency)
                self.assertIn("cancel-in-progress: false", concurrency)
                self.assertIn("if: ${{ github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success' }}", source)

    def test_hot_publisher_uses_latest_generation_when_dequeued(self) -> None:
        for file in TARGETED[1:]:
            with self.subTest(file=file):
                source = workflow(file)
                self.assertIn("git fetch --no-tags origin +main:refs/remotes/origin/main", source)
                self.assertIn("git reset --hard refs/remotes/origin/main", source)

    def test_targeted_routes_remain_automatic(self) -> None:
        stats = workflow(TARGETED[0])
        sim = workflow(TARGETED[1])
        self.assertIn("UEFA Current Stats Refresh", stats)
        self.assertIn("Domestic Enriched Cache Rebuild", stats)
        self.assertIn("API-Football Targeted Fixture Schedule Refresh", sim)
        self.assertIn("API-Football Standings Fetch", sim)


if __name__ == "__main__":
    unittest.main()
