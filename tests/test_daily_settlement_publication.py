"""Keep the app's result publication inside successful live settlement completion."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1] / '.github/workflows'


class DailyPublicationContractTest(unittest.TestCase):
    def setUp(self):
        self.live = (ROOT / 'live-settlement-publisher.yml').read_text()
        self.feed = (ROOT / 'shots-uat.yml').read_text()

    def test_daily_publication_is_a_required_job_after_result_collection(self):
        call = self.live.split('\n  publish-daily:\n', 1)[1]
        self.assertIn('needs: publish', call)
        self.assertIn('uses: ./.github/workflows/shots-uat.yml', call)
        self.assertIn('results_only: true', call)
        self.assertNotIn('continue-on-error:', call)
        self.assertNotIn('if: always()', call)

    def test_reused_feed_accepts_and_honours_typed_result_only_input(self):
        trigger = self.feed.split('  workflow_call:\n', 1)[1].split('  push:', 1)[0]
        self.assertRegex(trigger, r'results_only:[\s\S]*type: boolean')
        self.assertIn('default: false', trigger)
        flag = next(line for line in self.feed.splitlines() if 'RESULTS_ONLY:' in line)
        self.assertIn('inputs.results_only ||', flag)
        self.assertEqual(3, self.feed.count("if: env.RESULTS_ONLY != 'true'"))
        self.assertIn('python -m scripts.shots_uat.publish_production', self.feed)

    def test_live_completion_does_not_enqueue_a_second_copy_of_the_same_feed(self):
        triggers = self.feed.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertNotIn("'Live Settlement Publisher'", triggers)
        self.assertIn('Model Performance Settlement Refresh', triggers)
        self.assertIn('UEFA Current Stats Refresh', triggers)

    def test_caller_and_child_use_different_non_cancelling_locks(self):
        def group(source):
            return re.search(r'(?m)^  group: (.+)$', source).group(1)
        self.assertNotEqual(group(self.live), group(self.feed))
        self.assertIn('cancel-in-progress: false', self.live)
        self.assertIn('cancel-in-progress: false', self.feed)


if __name__ == '__main__':
    unittest.main()
