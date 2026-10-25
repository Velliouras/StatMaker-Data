"""Replay the real published v7 generation against the workflow's final verifier."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/app-ready-artifact-publisher.yml'
FIXTURE = ROOT / 'tests/fixtures/app_ready_published_contract_v7.json'


class AppReadyPublishedVerifierTest(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text())
        source = WORKFLOW.read_text().split('      - name: Verify published generation', 1)[1]
        self.code = textwrap.dedent(source.split("          python - <<'PY'\n", 1)[1]
                                   .split('\n          PY', 1)[0])

    def verify(self, fixture):
        def read(path, *args, **kwargs):
            key = 'status' if path.name == 'publish_status.json' else 'manifest'
            return json.dumps(fixture[key])
        metadata = self.fixture['manifest']['metadata']
        env = {'EXPECTED_STATMAKER_COMMIT': metadata['statmakerCommit'],
               'APP_READY_PATTERN_RULES_FINGERPRINT': metadata['preparedPatternRulesFingerprint']}
        with mock.patch.object(Path, 'read_text', read), mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(io.StringIO()):
            exec(compile(self.code, str(WORKFLOW), 'exec'), {})

    def test_real_v7_generation_accepted_with_the_staged_source_contract(self):
        self.verify(self.fixture)

    def test_different_rules_are_still_rejected(self):
        fixture = copy.deepcopy(self.fixture)
        fixture['manifest']['metadata']['preparedPatternRulesFingerprint'] = \
            'pattern-policy-v2-final-read-model-v6-probability-first-prod'
        with self.assertRaises(AssertionError):
            self.verify(fixture)

    def test_unmatched_status_is_still_rejected(self):
        fixture = copy.deepcopy(self.fixture)
        fixture['status']['contentVersion'] = 'other-generation'
        with self.assertRaises(AssertionError):
            self.verify(fixture)


if __name__ == '__main__':
    unittest.main()
