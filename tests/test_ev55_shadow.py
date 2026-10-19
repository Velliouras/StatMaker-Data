import sys,unittest,json,tempfile,shutil
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2 import forward_ev55_shadow as trial

class EV55ShadowTests(unittest.TestCase):
    def setup(self,td):
        source=trial.PARENT;index=trial.read(source/'index.json');item=index['cohorts'][0];doc=trial.read(source/item['path']);parent=Path(td)/'parent';parent.mkdir();shutil.copy2(source/item['path'],parent/item['path']);proof=trial.read(source/'publication_evidence.json')
        (parent/'index.json').write_text(json.dumps(dict(cohorts=[item])));(parent/'publication_evidence.json').write_text(json.dumps({item['path']:proof[item['path']]}));config=dict(candidateId='TEST-RESEARCH-ONLY',parameters=doc['parameters'],freezeBeforeUTC='2026-11-01T00:00:00Z');cp=Path(td)/'candidate.json';cp.write_text(json.dumps(config));out=Path(td)/'out';out.mkdir();clock=datetime.fromisoformat(doc['frozenAtUTC']).timestamp()+60
        class FixedDateTime(datetime):
            @classmethod
            def now(cls,tz=None):return cls.fromtimestamp(clock,tz or timezone.utc)
        return parent,config,cp,out,FixedDateTime,item,doc
    def test_research55_reuses_original_winner_and_source_stays_immutable(self):
        with tempfile.TemporaryDirectory() as td:
            parent,c,cp,out,clock,item,original=self.setup(td);raw=(parent/item['path']).read_bytes();index=dict(cohorts=[],sourceCohorts=[],marketKeys=[])
            with patch.object(trial,'PARENT',parent),patch.object(trial,'CONFIG',cp),patch.object(trial,'datetime',clock):
                created,_=trial.freeze(out,index,c);self.assertEqual(len(created),1);self.assertEqual(trial.freeze(out,index,c)[0],[])
            doc=trial.read(out/created[0]);self.assertEqual(len(doc['models']['residual_point']['selected']),2);self.assertEqual(len(doc['models']['residual_conservative']['selected']),1)
            self.assertEqual(original['models']['residual_point']['selected'],[]);self.assertEqual((parent/item['path']).read_bytes(),raw)
            keys={trial.key(q) for q in original['models']['stat_point']['selected']}
            for q in doc['models']['residual_point']['selected']:
                self.assertIn(trial.key(q),keys);self.assertGreaterEqual(q['p'],.55);self.assertFalse(q['certifiedStrong']);self.assertEqual(q['strategyClass'],'RESEARCH_EV55')
    def test_modified_parent_forecast_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            parent,c,cp,out,clock,item,_=self.setup(td);(parent/item['path']).write_bytes(b'changed')
            with patch.object(trial,'PARENT',parent),patch.object(trial,'CONFIG',cp),patch.object(trial,'datetime',clock):
                with self.assertRaises(ValueError):trial.freeze(out,dict(cohorts=[]),c)
    def test_missing_source_proof_cannot_create_projection(self):
        with tempfile.TemporaryDirectory() as td:
            parent,c,cp,out,clock,_,_=self.setup(td);(parent/'publication_evidence.json').write_text('{}')
            with patch.object(trial,'PARENT',parent),patch.object(trial,'CONFIG',cp),patch.object(trial,'datetime',clock):self.assertEqual(trial.freeze(out,dict(cohorts=[]),c)[0],[])
    def test_future_dated_proof_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            parent,c,cp,out,clock,item,_=self.setup(td);proof=trial.read(parent/'publication_evidence.json');proof[item['path']]['publiclyVerifiedAtUTC']='2030-01-01T00:00:00Z';(parent/'publication_evidence.json').write_text(json.dumps(proof))
            with patch.object(trial,'PARENT',parent),patch.object(trial,'CONFIG',cp),patch.object(trial,'datetime',clock):
                with self.assertRaises(ValueError):trial.freeze(out,dict(cohorts=[]),c)

if __name__=='__main__':unittest.main()
