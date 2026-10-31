from pathlib import Path
import unittest
import yaml


class CompatibleProducerPinTest(unittest.TestCase):
    def test_nightly_exporter_does_not_follow_the_stats_only_android_main(self):
        source = (Path(__file__).resolve().parents[1] / '.github/workflows/app-ready-artifact-publisher.yml').read_text()
        doc = yaml.safe_load(source)
        expected = 'b4630638077b1c3dd83d94c66c03e2a70f45efd1'
        self.assertEqual(expected, doc['env']['APP_READY_COMPAT_PRODUCER_REF'])
        events = doc.get('on', doc.get(True))
        self.assertEqual(expected, events['workflow_dispatch']['inputs']['statmaker_ref']['default'])
        steps = doc['jobs']['publish']['steps']
        preflight = next(s for s in steps if s.get('name') == 'Check source versions')
        checkout = next(s for s in steps if s.get('with', {}).get('repository') == 'Velliouras/StatMaker')
        self.assertIn('env.APP_READY_COMPAT_PRODUCER_REF', preflight['env']['STATMAKER_REF'])
        self.assertEqual(preflight['env']['STATMAKER_REF'], checkout['with']['ref'])
        self.assertNotIn("inputs.statmaker_ref || 'main'", source)

    def test_pipeline_validator_uses_the_same_compatible_producer_as_the_publisher(self):
        root = Path(__file__).resolve().parents[1] / '.github/workflows'
        publisher = yaml.safe_load((root / 'app-ready-artifact-publisher.yml').read_text())
        validation = yaml.safe_load((root / 'app-ready-pipeline-validation.yml').read_text())
        job = validation['jobs']['validate']
        self.assertEqual(publisher['env']['APP_READY_COMPAT_PRODUCER_REF'],
                         job['env']['APP_READY_COMPAT_PRODUCER_REF'])
        checkout = next(step for step in job['steps']
                        if step.get('with', {}).get('repository') == 'Velliouras/StatMaker')
        self.assertEqual('${{ env.APP_READY_COMPAT_PRODUCER_REF }}', checkout['with']['ref'])


if __name__ == '__main__':
    unittest.main()
