"""Regression contracts for quota-safe statistics automation after betting retirement."""
from pathlib import Path
import json
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def source(filename):
    return (WORKFLOWS / filename).read_text(encoding="utf-8")


def event_section(filename):
    text = source(filename)
    return text.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]


class StatsWatchdogTest(unittest.TestCase):
    def test_stats_watchdog_keeps_regular_refreshes_and_no_settlement_dispatch(self):
        workflow = source("live-settlement-watchdog.yml")
        events = event_section("live-settlement-watchdog.yml")
        self.assertIn("Stats Freshness and API Quota Watchdog", workflow)
        self.assertIn('cron: "8,23,38,53 * * * *"', events)
        self.assertIn("workflow_run:", events)
        self.assertIn('workflows: ["Priority Results and Official Standings"]', events)
        self.assertNotIn("Live Settlement Publisher", events)
        self.assertNotIn("live-settlement-publisher.yml/dispatches", workflow)
        self.assertNotIn("Maintain live settlement cadence", workflow)
        self.assertIn("quota-usage-snapshot:", workflow)

    def test_stats_fanout_preserves_scorers_results_and_uefa(self):
        workflow = source("live-settlement-watchdog.yml")
        for name in (
            "priority-results-standings.yml",
            "domestic-results-context-refresh.yml",
            "schedule-scorers-manual.yml",
            "league-tables-program-publish.yml",
        ):
            self.assertIn("uses: ./.github/workflows/" + name, workflow)
        self.assertIn("max_requests: '6'", workflow)
        self.assertIn("lookback: '14'", workflow)
        self.assertIn("phase: leaders", workflow)
        self.assertIn("refresh-postmatch-statistics:", workflow)
        self.assertIn("refresh-postmatch-fallback:", workflow)
        self.assertIn("uses: ./.github/workflows/recent-postmatch-stats.yml", workflow)
        self.assertIn("needs: refresh-priority-results-and-standings", workflow)

    def test_provider_budget_is_fail_closed_with_1500_reserve(self):
        watchdog = source("live-settlement-watchdog.yml")
        self.assertIn('STATMAKER_API_BUDGET_ENFORCE: "true"', watchdog)
        self.assertIn('STATMAKER_API_BUDGET_PROFILE: background', watchdog)
        self.assertIn("scripts/api_football_usage.py --reserve 1500", watchdog)
        policy = json.loads((ROOT / "config/api_football_refresh_policy.json").read_text())
        self.assertEqual(policy["totalStop"], 6000)
        self.assertEqual(policy["backgroundStop"], 4500)
        self.assertEqual(policy["reserve"], 1500)
        guard = (ROOT / "scripts/api_budget_runtime/api_football_budget.py").read_text()
        self.assertIn("TOTAL_STOP = 6000", guard)
        self.assertIn("BACKGROUND_STOP = 4500", guard)
        self.assertIn("RESERVE = 1500", guard)

    def test_old_betting_jobs_are_manual_only(self):
        for filename in (
            "live-settlement-publisher.yml",
            "odds-integrity-production.yml",
            "model-performance-settlement-refresh.yml",
            "market-watch.yml",
            "canonical-prod-value-first-publisher.yml",
            "betting-v2-forward-candidate.yml",
            "betting-v2-forward-context.yml",
            "betting-v2-forward-market-first.yml",
            "betting-v2-forward-market-residual.yml",
        ):
            events = event_section(filename)
            with self.subTest(workflow=filename):
                self.assertIn("workflow_dispatch:", events)
                self.assertNotIn("schedule:", events)
                self.assertNotIn("workflow_run:", events)
                self.assertNotIn("push:", events)

    def test_match_stats_have_two_small_guarded_refresh_windows(self):
        workflow = source("domestic-targeted-stats-backfill.yml")
        events = event_section("domestic-targeted-stats-backfill.yml")
        self.assertIn("cron: '35 22 * * *'", events)
        self.assertIn("cron: '30 7 * * *'", events)
        self.assertIn('codes="G1,E0,SP1"; max_requests="48"', workflow)
        self.assertIn('codes="I1,D1,F1"; max_requests="48"', workflow)
        self.assertIn('STATMAKER_API_BUDGET_PROFILE: background', workflow)
        self.assertIn('STATMAKER_BETTING_V2_CAPTURE_LINEAGE: "0"', workflow)

    def test_app_ready_stats_publisher_does_not_rebuild_betting(self):
        workflow = source("app-ready-stats-hot-publish.yml")
        self.assertIn("python scripts/hotpatch_app_ready_stats.py", workflow)
        self.assertNotIn("betting_v2.publish_uat_feed", workflow)


if __name__ == "__main__":
    unittest.main()
