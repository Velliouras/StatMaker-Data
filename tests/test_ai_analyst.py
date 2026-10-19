"""Isolation, temporal validity, budget, transport and strict review validation."""
import copy
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from ai_analyst.contracts import (build_packets, canonical, digest, recent_stats,
                                utc, validate_review)
from ai_analyst.provider import infer, ProviderError, NoRedirect
from ai_analyst.publish import merge
from ai_analyst.run_shadow import run, score, write, OUTPUT

POLICY = Path(__file__).resolve().parents[1]/'research/ai_analyst/v1/policy.json'
NOW = 1800000000.


def answer(analysis):
    return dict(schemaVersion=1, fixtureRef=analysis['fixtureRef'],
                dataAssessment=dict(level='LIMITED', factIds=['F001'], note='Λείπουν επιβεβαιωμένες ενδεκάδες.'),
                reviews=[dict(offerId=r['offerId'], verdict='CAUTION', reasonCodes=['COVERAGE_LIMITED'],
                              factIds=['F001', r['factId']], note='Τα διαθέσιμα στοιχεία απαιτούν επιφύλαξη.')
                         for r in analysis['offers']])


class AnalystTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = json.loads(POLICY.read_text())
        self.folder = self.config['sources'][0]['directory']
        self.make('research/ai_analyst/v1/policy.json', self.config)
        self.make(self.config['historyPath'], [])
        candidate = dict(candidateId='test-source')
        self.make(self.folder+'/candidate.json', candidate)
        candidate_hash = digest((self.root/self.folder/'candidate.json').read_bytes())
        self.row = dict(fixture='test-fixture', _home='home', _away='away',
                        kickoff=NOW+3600, observed=NOW-300, featureCutoff=(int(NOW)//86400-1)*86400,
                        spec=['SHOTS', 'TEAM', 'OVER', 'HOME', 10.5], odd=2., p=.62,
                        push=0., ev=.24, bookmaker='TestBook', sourceReceipt='receipt')
        self.doc = dict(candidateId='test-source', candidateSha256=candidate_hash,
                        frozenAtUTC=utc(NOW-200), models={'residual_conservative':
                        dict(forecasts=[self.row], selected=[self.row])})
        self.name = 'frozen_test.json.gz'
        self.refresh()

    def tearDown(self):
        self.temp.cleanup()

    def make(self, path, value):
        p = self.root/path
        p.parent.mkdir(parents=True, exist_ok=True)
        raw = canonical(value)
        p.write_bytes(gzip.compress(raw, mtime=0) if p.suffix == '.gz' else raw)

    def refresh(self):
        self.make(self.folder+'/'+self.name, self.doc)
        sha = digest((self.root/self.folder/self.name).read_bytes())
        self.make(self.folder+'/index.json', dict(cohorts=[dict(path=self.name, sha256=sha)]))
        self.make(self.folder+'/publication_evidence.json',
                  {self.name: dict(sha256=sha, publiclyVerifiedAtUTC=utc(NOW-100))})

    def fake(self, body, token):
        return answer(json.loads(body['messages'][1]['content'])), dict(model='test', usage={})

    def run_live(self, **kwargs):
        return run(self.root, NOW, True, 'test-token', kwargs.pop('inference', self.fake), lambda: NOW+1, **kwargs)

    def test_real_source_inputs_are_unchanged_and_no_core_import_is_needed(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        status = self.run_live()
        self.assertEqual(status['llmCallsThisRun'], 1)
        for p, raw in before.items():
            self.assertEqual((self.root/p).read_bytes(), raw)
        result = json.loads(next((self.root/OUTPUT/'reviews').glob('*.json')).read_bytes())
        self.assertIsNone(result['reliabilityScore'])
        self.assertTrue(result['baselineProbabilitiesUnchanged'])
        self.assertFalse(result['automaticPromotion'])

    def test_preview_never_calls_provider_or_fabricates_review(self):
        with patch('ai_analyst.provider.urllib.request.build_opener', side_effect=AssertionError('network')):
            status = run(self.root, NOW)
        self.assertEqual(status['status'], 'PREVIEW_ONLY')
        self.assertFalse((self.root/OUTPUT/'reviews').exists())

    def test_existing_review_is_not_called_again(self):
        self.run_live()
        status = self.run_live(inference=lambda *_: self.fail('duplicate inference'))
        self.assertEqual(status['llmCallsThisRun'], 0)

    def test_daily_cap_includes_failed_attempts(self):
        for i in range(1, 4):
            row = dict(self.row, fixture='fixture-'+str(i), kickoff=NOW+3600+i)
            self.doc['models']['residual_conservative']['forecasts'].append(row)
            self.doc['models']['residual_conservative']['selected'].append(row)
        self.refresh()
        def fail(*_):
            raise ProviderError('MODELS_HTTP_429')
        status = self.run_live(inference=fail)
        self.assertEqual(status['llmCallsThisRun'], 1)
        self.assertEqual(status['remainingDailyRequests'], 1)
        self.assertEqual(self.run_live(inference=fail)['remainingDailyRequests'], 0)
        self.assertEqual(self.run_live()['llmCallsThisRun'], 0)
        self.assertFalse((self.root/OUTPUT/'reviews').exists())

    def test_invalid_response_cannot_change_probability_or_add_offer(self):
        packet = build_packets(self.root, self.config, NOW)[0][0]
        review = answer(packet['analysis'])
        review['reviews'][0]['winProbability'] = .99
        with self.assertRaises(ValueError):
            validate_review(review, packet)
        review = answer(packet['analysis'])
        review['reviews'][0]['offerId'] = 'invented'
        with self.assertRaises(ValueError):
            validate_review(review, packet)

    def test_unsupported_narrative_numbers_and_fact_ids_are_rejected(self):
        packet = build_packets(self.root, self.config, NOW)[0][0]
        for note, refs in [('Πιθανότητα 99%', ['F001']), ('Ανύπαρκτο στοιχείο.', ['F999'])]:
            review = answer(packet['analysis'])
            review['dataAssessment'].update(note=note, factIds=refs)
            with self.assertRaises(ValueError):
                validate_review(review, packet)

    def test_missing_or_future_publication_does_not_reach_provider(self):
        self.make(self.folder+'/publication_evidence.json', {})
        self.assertEqual(self.run_live()['readyFixtures'], 0)
        self.refresh()
        p = self.root/self.folder/'publication_evidence.json'
        d = json.loads(p.read_text()); d[self.name]['publiclyVerifiedAtUTC'] = utc(NOW+20)
        self.make(self.folder+'/publication_evidence.json', d)
        with self.assertRaises(ValueError):
            self.run_live()

    def test_source_hash_mismatch_is_rejected(self):
        self.doc['models']['residual_conservative']['selected'][0]['p'] = .99
        self.make(self.folder+'/'+self.name, self.doc)
        with self.assertRaises(ValueError):
            self.run_live()

    def test_live_or_stale_fixtures_are_not_reviewed(self):
        for changes in [dict(kickoff=NOW-1), dict(observed=NOW-90000)]:
            row = dict(self.row, **changes)
            self.doc['models']['residual_conservative'] = dict(forecasts=[row], selected=[row])
            self.refresh()
            self.assertEqual(self.run_live()['readyFixtures'], 0)

    def test_future_history_is_excluded_and_teams_are_anonymized(self):
        history = [dict(_home='home', _away='other', _date=NOW+100, normalized_stats={'HS':999,'AS':999})]
        self.make(self.config['historyPath'], history)
        packet = build_packets(self.root, self.config, NOW)[0][0]
        self.assertFalse(any(f['kind'] == 'PRIOR_COUNTS' for f in packet['analysis']['facts']))
        self.assertNotIn('test-fixture', canonical(packet['analysis']).decode())

    def test_writer_refuses_engine_and_app_paths(self):
        for path in ['reports/betting_v2/hijack.json', 'data/statmaker/latest.json',
                     OUTPUT+'/../../betting_v2/hijack.json']:
            with self.assertRaises(ValueError):
                write(self.root, path, {})

    def test_model_output_is_not_scoreable_before_own_publication(self):
        self.run_live()
        result = score(self.root, NOW+10)
        self.assertEqual(result['awaitingPublication'], 1)
        self.assertEqual(result['groups']['AI_CAUTION']['bets'], 0)

    def test_same_baseline_labels_grade_each_ai_subset_without_changing_original(self):
        self.run_live()
        path = next((self.root/OUTPUT/'reviews').glob('*.json'))
        self.make(OUTPUT+'/publication_evidence.json', {path.relative_to(self.root).as_posix():
                  dict(sha256=digest(path.read_bytes()), publiclyVerifiedAtUTC=utc(NOW+2))})
        source = json.loads((self.root/self.folder/'index.json').read_text())['cohorts'][0]
        self.make(self.folder+'/'+self.name.replace('.json.gz','_score.json'),
                  dict(frozenSha256=source['sha256'], publicationVerified=True,
                       models={'residual_conservative':dict(selectedRows=[dict(self.row,y=1.,actualPush=0.)])}))
        result = score(self.root, NOW+4000)
        for key in ('BASELINE_SAME_REVIEWED_FIXTURES','AI_CAUTION'):
            self.assertEqual(result['groups'][key]['bets'], 1)
            self.assertEqual(result['groups'][key]['profitUnits'], 1.)
        self.assertEqual(result['groups']['AI_SUPPORT']['bets'], 0)

    def test_late_ai_publication_cannot_claim_prospective_results(self):
        self.run_live()
        path = next((self.root/OUTPUT/'reviews').glob('*.json'))
        self.make(OUTPUT+'/publication_evidence.json', {path.relative_to(self.root).as_posix():
                  dict(sha256=digest(path.read_bytes()), publiclyVerifiedAtUTC=utc(NOW+3601))})
        self.assertEqual(score(self.root, NOW+4000)['excludedLatePublication'], 1)

    def test_publisher_preserves_immutable_records_and_rejects_other_paths(self):
        a = self.root/'incoming'
        (a/'reviews').mkdir(parents=True)
        (a/'reviews/test.json').write_text('{}')
        self.assertEqual(merge(self.root,a), 1)
        (a/'reviews/test.json').write_text('{"changed":true}')
        with self.assertRaises(ValueError):
            merge(self.root,a)
        (a/'reviews/test.json').unlink()
        (a/'unexpected.json').write_text('{}')
        with self.assertRaises(ValueError):
            merge(self.root,a)


class TransportTests(unittest.TestCase):
    def test_no_token_or_http_failure_does_not_fake_ai_success_or_expose_secret(self):
        with self.assertRaises(ProviderError):
            infer({}, '')
        class Client:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url,403,'secret-bearing body',{},io.BytesIO(b'secret'))
        with self.assertRaisesRegex(ProviderError, '^MODELS_HTTP_403$'):
            infer({}, 'never-log-this', Client())

    def test_response_parsing_records_only_allowlisted_usage(self):
        content = dict(schemaVersion=1)
        class Client:
            def open(self, request, timeout):
                self.request = request
                return io.BytesIO(canonical(dict(choices=[dict(finish_reason='stop',message=dict(content=json.dumps(content)))],
                        usage=dict(prompt_tokens=100, completion_tokens=40, secret='hidden'))))
        review, metadata = infer({'model':'test'}, 'secret', Client())
        self.assertEqual(review, content)
        self.assertEqual(metadata['usage'], dict(prompt_tokens=100, completion_tokens=40))

    def test_credential_cannot_follow_redirect(self):
        with self.assertRaises(ProviderError):
            NoRedirect().redirect_request(None,None,302,'',{},'https://elsewhere.invalid')


if __name__ == '__main__':
    unittest.main()
