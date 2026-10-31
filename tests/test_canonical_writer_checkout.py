"""Serialized producers must resolve data after acquiring the lock, not at event time."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1] / '.github/workflows'


class CanonicalWriterCheckoutTest(unittest.TestCase):
    def test_each_serialized_writer_starts_from_current_main(self):
        checked = 0
        for path in sorted(ROOT.glob('*.y*ml')):
            workflow = yaml.safe_load(path.read_text())
            if (workflow.get('concurrency') or {}).get('group') != 'statmaker-canonical-main-writer':
                continue
            for name, job in workflow.get('jobs', {}).items():
                checkout = next((step for step in job.get('steps', [])
                                 if str(step.get('uses', '')).startswith('actions/checkout@')), None)
                if checkout is None:
                    continue
                with self.subTest(workflow=path.name, job=name):
                    self.assertEqual('main', (checkout.get('with') or {}).get('ref'),
                                     'A queued trigger SHA can overwrite/conflict with the previous serialized producer')
                checked += 1
        self.assertGreater(checked, 0)

    def test_near_kickoff_context_writers_do_not_download_full_snapshot_history(self):
        for name in ('domestic-lineup-watch.yml', 'domestic-match-context.yml'):
            workflow = yaml.safe_load((ROOT / name).read_text())
            for job in workflow['jobs'].values():
                checkout = next(step for step in job.get('steps', [])
                                if str(step.get('uses', '')).startswith('actions/checkout@'))
                self.assertEqual(1, checkout['with']['fetch-depth'])
                self.assertEqual('blob:none', checkout['with']['filter'])

    def test_secondary_fixture_calendar_branch_routes_remain_explicit(self):
        workflow = yaml.safe_load((ROOT / 'fixture-calendar-metadata.yml').read_text())
        checkouts = [step for job in workflow['jobs'].values() for step in job.get('steps', [])
                     if str(step.get('uses', '')).startswith('actions/checkout@')]
        self.assertEqual(['main', '${{ matrix.branch }}'], [step['with']['ref'] for step in checkouts])


if __name__ == '__main__':
    unittest.main()
