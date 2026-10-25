"""Exercise the keeper's actual dispatch code without GitHub or provider requests."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/live-settlement-watchdog.yml'


class SettlementWatchdogTest(unittest.TestCase):
    def run_keeper(self, runs):
        source = WORKFLOW.read_text()
        code = textwrap.dedent(source.split("          python - <<'PY'\n", 1)[1]
                               .split('\n          PY', 1)[0])
        dispatches = []

        def open_url(request, timeout):
            if request.get_method() == 'POST':
                dispatches.append(json.loads(request.data))
                response = io.StringIO('')
                response.status = 204
            else:
                response = io.StringIO(json.dumps({'workflow_runs': runs}))
                response.status = 200
            return response

        env = {'GH_TOKEN': 'test-token', 'API_URL': 'https://api.github.com',
               'REPOSITORY': 'example/StatMaker-Data'}
        with mock.patch.dict(os.environ, env), \
                mock.patch('urllib.request.urlopen', side_effect=open_url), \
                contextlib.redirect_stdout(io.StringIO()):
            exec(compile(code, str(WORKFLOW), 'exec'), {})
        return dispatches

    def publisher(self, age=3, status='completed', conclusion='success'):
        timestamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=age)).isoformat()
        return {'id': 123, 'status': status, 'conclusion': conclusion,
                'created_at': timestamp, 'updated_at': timestamp}

    def test_missed_schedules_dispatch_one_bounded_recovery(self):
        self.assertEqual([{'ref': 'main', 'inputs': {'max_requests': '80'}}],
                         self.run_keeper([self.publisher(age=180)]))

    def test_fresh_publisher_never_consumes_another_provider_budget(self):
        self.assertEqual([], self.run_keeper([self.publisher(age=3)]))
        long_run = self.publisher(age=180)
        long_run['updated_at'] = self.publisher(age=3)['updated_at']
        self.assertEqual([], self.run_keeper([long_run]))

    def test_queued_or_running_publisher_is_not_duplicated_even_when_old(self):
        for status in ('queued', 'in_progress', 'waiting', 'requested', 'pending'):
            with self.subTest(status=status):
                self.assertEqual([], self.run_keeper([
                    self.publisher(age=180, status=status, conclusion=None)]))

    def test_failed_publisher_and_first_boot_are_recovered(self):
        self.assertEqual(1, len(self.run_keeper([self.publisher(conclusion='failure')])))
        self.assertEqual(1, len(self.run_keeper([])))

    def test_fallback_comes_from_independent_producers_without_a_settlement_loop(self):
        source = WORKFLOW.read_text()
        triggers = source.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertIn('workflow_run:', triggers)
        self.assertIn('Canonical PROD Value-First Publisher', triggers)
        self.assertIn('Odds Integrity Production Refresh', triggers)
        self.assertNotIn('Live Settlement Publisher', triggers)
        self.assertNotIn('Shots SOT UAT Feed', triggers)
        self.assertIn('cron: "8,23,38,53 * * * *"', triggers)


if __name__ == '__main__':
    unittest.main()
