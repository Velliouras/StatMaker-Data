import sys,unittest,json,gzip,tempfile
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.forward_context import (market_times,offer_key,attach_market_times,evaluate_group,score,
    MonthlyModels,ROOT,PRIOR,partitions,conservative_winner,read,digest)
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.context_market import METRICS


class ForwardContextTests(unittest.TestCase):
    def receipt(self,when='1970-01-02T00:00:00Z'):
        return dict(boundedMarketSnapshot=[dict(event=dict(id='1'),bookmakers=[dict(bookmaker='A',markets=[dict(name='Total',updatedAt=when,
            outcomes=[dict(id='o',hdp=9.5,over=2.,under=1.9)])])])])

    def quote(self):
        return dict(sourceReceipt='r.json.gz',sourceSha256='',providerEventId='1',bookmaker='A',providerMarketName='Total',
            selection='OVER',providerSelectionId='o',line=9.5,odd=2.,observed=90000.)

    def attach(self,doc,quote=None):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'r.json.gz';path.write_bytes(gzip.compress(json.dumps(doc).encode()))
            q=self.quote() if quote is None else quote;q['sourceSha256']=digest(path.read_bytes())
            return attach_market_times([q],Path(d))

    def test_timestamp_is_matched_to_exact_offer(self):
        result,audit=self.attach(self.receipt())
        self.assertEqual(len(result),1);self.assertEqual(audit,{})
        self.assertEqual(result[0]['marketUpdatedAtUTC'],'1970-01-02T00:00:00+00:00')
        self.assertEqual(self.attach(self.receipt(),dict(self.quote(),odd=2.1))[0],[])

    def test_missing_future_and_stale_times_are_never_invented(self):
        self.assertEqual(self.attach(self.receipt(None))[0],[])
        self.assertEqual(self.attach(self.receipt('1970-01-03T00:00:00Z'))[0],[])
        self.assertEqual(self.attach(self.receipt('1970-01-01T00:00:01Z'))[0],[])

    def test_receipt_hash_mismatch_blocks_freeze(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'r.json.gz').write_bytes(gzip.compress(json.dumps(self.receipt()).encode()))
            with self.assertRaises(ValueError):attach_market_times([self.quote()],Path(d))

    def test_conflicting_timestamp_aliases_are_rejected(self):
        d=self.receipt();d['boundedMarketSnapshot'][0]['bookmakers'][0]['markets'][0]['lastUpdate']='1970-01-02T01:00:00Z'
        self.assertEqual(self.attach(d)[0],[])

    def test_forward_predictions_reproduce_research_point_and_lower(self):
        source=ROOT/'reports/betting_v2/context_market_model_20261018';doc=read(source/'priced_latest.json')
        candidates=doc['reports']['context_conservative']['picks']
        chosen=next((r for r in candidates if r['featureCutoff']>=1790812800),candidates[0])
        quotes=[q for directory in (PRIOR,ROOT/'reports/betting_v2/git_archive_expansion_20261016') for q in read(directory/'quotes_latest.json.gz')
            if q['fixture']==chosen['fixture'] and q['sourceReceipt']==chosen['sourceReceipt']]
        groups,_=partitions(quotes)
        g=next(g for g in groups if g['over']['bookmaker']==chosen['bookmaker'] and g['over']['spec'][0:2]==chosen['spec'][0:2]
            and g['over']['spec'][3:]==chosen['spec'][3:])
        profiles={day:{m:read(PRIOR/f'{m}_{day}_coupled_profiles.json')['profiles'] if day!='2026-10-01'
            else read(ROOT/f'reports/betting_v2/combined_candidate_20261016/{m}_profiles.json') for m in METRICS}
            for day in ('2026-08-01','2026-09-01','2026-10-01')}
        models=MonthlyModels(read(PRIOR/'history.json.gz'),profiles);models.advance(chosen['featureCutoff'])
        parameters=read(source/'parameter_banks_latest.json')['fits'][models.fold]
        fav=fixture_favourite(quotes).get((chosen['fixture'],chosen['bookmaker'],chosen['sourceReceipt']))
        records,options=evaluate_group(models,g,parameters,fav)
        point=next(r for r in options['context'] if r['spec']==chosen['spec'])
        expected=next(r for r in doc['reports']['context_point']['picks'] if r['fixture']==chosen['fixture'] and r['bookmaker']==chosen['bookmaker'] and r['spec']==chosen['spec'])
        lower=conservative_winner(point)
        np.testing.assert_allclose([point['p'],lower['p'],lower['push'],lower['confidence']],
            [expected['p'],chosen['p'],chosen['push'],chosen['confidence']],rtol=0,atol=1e-12)

    def setup_snapshot(self,out,proof_time=9999.):
        q=dict(fixture='10000:h:a',kickoff=10000.,observed=1.,bookmaker='A',spec=['SOT','TOTAL','OVER','',4.5],
            p=.65,push=0.,odd=2.,confidence=.65,winProbability=.65,pushProbability=0.)
        doc=dict(frozenAtUTC=datetime.fromtimestamp(2.,timezone.utc).isoformat(),models={'context_conservative':dict(selected=[q],forecasts=[q])})
        raw=gzip.compress(json.dumps(doc).encode());(out/'frozen.json.gz').write_bytes(raw)
        item=dict(path='frozen.json.gz',sha256=digest(raw));proof=dict(sha256=item['sha256'],publiclyVerifiedAtUTC=datetime.fromtimestamp(proof_time,timezone.utc).isoformat())
        (out/'publication_evidence.json').write_text(json.dumps({'frozen.json.gz':proof}))
        return dict(cohorts=[item])

    def test_publication_at_kickoff_is_excluded_and_missing_labels_stay_pending(self):
        with tempfile.TemporaryDirectory() as d,patch('betting_v2.forward_context.direct.load_actual_results',return_value={'GLOBAL':[]}):
            out=Path(d);index=self.setup_snapshot(out,10000.);score(out,index)
            s=read(out/'summary.json');self.assertEqual(s['publicBeforeKickoffFixtures'],0)
            index=self.setup_snapshot(out,9999.);score(out,index)
            s=read(out/'summary.json');self.assertEqual(s['models']['context_conservative']['settled']['bets'],0)
            self.assertEqual(s['models']['context_conservative']['pendingEligible'],1)

    def test_snapshot_mutation_is_blocked(self):
        with tempfile.TemporaryDirectory() as d,patch('betting_v2.forward_context.direct.load_actual_results',return_value={'GLOBAL':[]}):
            out=Path(d);index=self.setup_snapshot(out);(out/'frozen.json.gz').write_bytes(b'changed')
            with self.assertRaises(ValueError):score(out,index)

    def test_duplicate_canonical_fixture_cohorts_are_blocked(self):
        with tempfile.TemporaryDirectory() as d,patch('betting_v2.forward_context.direct.load_actual_results',return_value={'GLOBAL':[]}):
            out=Path(d);index=self.setup_snapshot(out);item=index['cohorts'][0]
            (out/'second.json.gz').write_bytes((out/'frozen.json.gz').read_bytes());index['cohorts'].append(dict(item,path='second.json.gz'))
            proof=read(out/'publication_evidence.json');proof['second.json.gz']=proof['frozen.json.gz'];(out/'publication_evidence.json').write_text(json.dumps(proof))
            with self.assertRaises(ValueError):score(out,index)


if __name__=='__main__':unittest.main()
