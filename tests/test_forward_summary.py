import gzip,hashlib,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.forward_summary import aggregate,summarise


class ForwardSummaryTests(unittest.TestCase):
    def add(self,out,index,name,fixtures,profit):
        rows=[dict(fixture=f,kickoff=20.) for f in fixtures]
        doc=dict(models=dict(control=dict(selected=rows,forecasts=rows)))
        raw=gzip.compress(json.dumps(doc).encode());sha=hashlib.sha256(raw).hexdigest();path='frozen_'+name+'.json.gz'
        (out/path).write_bytes(raw);index['cohorts'].append(dict(path=path,sha256=sha))
        proof=dict(sha256=sha,publiclyVerifiedAtUTC='1970-01-01T00:00:10+00:00')
        proofs=json.loads((out/'publication_evidence.json').read_text()) if (out/'publication_evidence.json').exists() else {}
        proofs[path]=proof;(out/'publication_evidence.json').write_text(json.dumps(proofs))
        score=dict(frozenSha256=sha,publicationVerified=True,publicationEvidence=proof,
            models=dict(control=dict(selected=dict(bets=len(rows),fixtures=len(rows),profitUnits=profit,meanPredicted=.6))))
        (out/path.replace('.json.gz','_score.json')).write_text(json.dumps(score))

    def test_total_roi_uses_stakes_instead_of_mean_of_daily_percentages(self):
        result=aggregate([dict(bets=2,profitUnits=1.),dict(bets=1,profitUnits=-1.)])
        self.assertEqual(result['bets'],3);self.assertEqual(result['roi'],0.)

    def test_duplicate_fixture_across_cohorts_blocks_combined_roi(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder);index=dict(cohorts=[])
            self.add(out,index,'a',['f1','f2'],1.);self.add(out,index,'b',['f1'],-1.)
            result=summarise(out,index)
            self.assertEqual(result['overlappingFixtures'],['f1'])
            self.assertIsNone(result['models']['control']['settled'])

    def test_unpublished_snapshot_does_not_contribute_to_profit(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder);index=dict(cohorts=[]);self.add(out,index,'a',['f1'],1.)
            (out/'publication_evidence.json').unlink();result=summarise(out,index)
            self.assertEqual(result['models']['control']['settled']['bets'],0)
            self.assertEqual(result['cohorts'][0]['models']['control']['awaitingPublication'],1)

    def test_score_must_bind_the_exact_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder);index=dict(cohorts=[]);self.add(out,index,'a',['f1'],1.)
            path=out/'frozen_a_score.json';score=json.loads(path.read_text());score['frozenSha256']='wrong';path.write_text(json.dumps(score))
            with self.assertRaises(ValueError):summarise(out,index)


if __name__=='__main__':unittest.main()
