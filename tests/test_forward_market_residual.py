import sys,unittest,json,gzip,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2 import forward_market_residual as trial
from betting_v2.research_csv_prices import digest

class ForwardMarketResidualTests(unittest.TestCase):
    def q(self,metric='CORNERS',observed=100.,receipt='a',**kwargs):
        return dict(fixture='10000:h:a',kickoff=10000.,observed=observed,featureCutoff=0.,sourceReceipt=receipt,providerEventId='1',
            bookmaker='A',spec=[metric,'TOTAL','OVER','',4.5],odd=2.,**kwargs)

    def test_later_new_market_survives_first_fixture_receipt(self):
        rows=[self.q(),self.q('SHOTS',200.,'b'),self.q('CORNERS',200.,'b')]
        chosen=trial.first_markets(rows,set())
        self.assertEqual({q['spec'][0]:q['sourceReceipt'] for q in chosen},{'CORNERS':'a','SHOTS':'b'})

    def test_seen_metric_does_not_exclude_new_metric_on_same_fixture(self):
        rows=trial.first_markets([self.q(),self.q('FOULS')],{('10000:h:a','CORNERS')})
        self.assertEqual([q['spec'][0] for q in rows],['FOULS'])

    def test_favourite_comes_only_from_same_book_and_receipt(self):
        results=[dict(self.q(),spec=['GOALS_FT','RESULT',side,'',None],odd=odd) for side,odd in [('HOME',1.5),('DRAW',4.),('AWAY',6.)]]
        rows=trial.first_markets(results+[self.q(),self.q('SHOTS',200.,'b')],set())
        self.assertEqual(rows[0]['_favourite'],'HOME');self.assertIsNone(rows[1]['_favourite'])
        self.assertEqual(rows[1]['_sameReceiptResultOdds'],{})

    def test_conservative_abstains_without_replacing_point_winner(self):
        a=dict(self.q(),preference=1,confidence=.75,p=.75,push=0.,ev=.5,_lower=dict(p=.59,push=0.,support=None))
        b=dict(self.q(),spec=['CORNERS','TOTAL','OVER','',5.5],preference=1,confidence=.7,p=.7,push=0.,ev=.4,_lower=dict(p=.65,push=0.,support=None))
        arms=trial.selected_arms([a,b]);self.assertEqual(arms['point'],[a]);self.assertEqual(arms['conservative'],[])

    def snapshot(self,out,metric='CORNERS',name='a.json.gz',proof=9999.):
        q=self.q(metric,p=.65,push=0.,winProbability=.65,pushProbability=0.)
        doc=dict(frozenAtUTC='1970-01-01T00:33:20+00:00',marketKeys=[[q['fixture'],metric]],models={'residual_conservative':dict(selected=[q],forecasts=[q])})
        raw=gzip.compress(json.dumps(doc).encode());(out/name).write_bytes(raw)
        item=dict(path=name,sha256=digest(raw));evidence=trial.read(out/'publication_evidence.json') if (out/'publication_evidence.json').exists() else {}
        from datetime import datetime,timezone
        evidence[name]=dict(sha256=item['sha256'],publiclyVerifiedAtUTC=datetime.fromtimestamp(proof,timezone.utc).isoformat())
        (out/'publication_evidence.json').write_text(json.dumps(evidence));return item

    @patch.object(trial.direct,'load_actual_results',return_value={'GLOBAL':[]})
    def test_publication_gate_and_pending_outcomes(self,_):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);item=self.snapshot(out,proof=10000.);trial.score(out,dict(cohorts=[item]))
            self.assertEqual(trial.read(out/'summary.json')['publicBeforeKickoffFixtures'],0)
            item=self.snapshot(out);trial.score(out,dict(cohorts=[item]))
            model=trial.read(out/'summary.json')['models']['residual_conservative']
            self.assertEqual(model['settled']['bets'],0);self.assertEqual(model['pendingEligible'],1)

    @patch.object(trial.direct,'load_actual_results',return_value={'GLOBAL':[]})
    def test_different_metric_cohorts_allowed_same_metric_rejected(self,_):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);a=self.snapshot(out);b=self.snapshot(out,'SHOTS','b.json.gz')
            trial.score(out,dict(cohorts=[a,b]))
            c=self.snapshot(out,'CORNERS','c.json.gz')
            with self.assertRaises(ValueError):trial.score(out,dict(cohorts=[a,c]))

    @patch.object(trial.direct,'load_actual_results',return_value={'GLOBAL':[]})
    def test_immutable_snapshot_bytes_are_enforced(self,_):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);item=self.snapshot(out);(out/item['path']).write_bytes(b'changed')
            with self.assertRaises(ValueError):trial.score(out,dict(cohorts=[item]))

if __name__=='__main__':unittest.main()
