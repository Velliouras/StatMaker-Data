"""Independent publication-chain, fixed-winner and EV55 threshold verification."""
import argparse,sys,gzip,json,math
from pathlib import Path
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import ROOT
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.audit_ev55_sensitivity import adjusted,conservative,key
from betting_v2.forward_candidate import public_rows


def main(out,pinned):
    config=read(out/'candidate.json');assert all(digest((ROOT/p).read_bytes())==h for p,h in config['files'].items());index=read(out/'index.json');proof=read(out/'publication_evidence.json');parent=ROOT/'reports/betting_v2/forward_market_residual_20261019';banks=read(ROOT/config['parameters']['path'])['fits']['earliest']['2026-10-19'];seen=set();cohorts=[]
    for item in index['cohorts']:
        raw=(out/item['path']).read_bytes();assert digest(raw)==item['sha256']==proof[item['path']]['sha256'];doc=read(out/item['path']);source_bytes=(parent/doc['sourceCohort']).read_bytes();assert digest(source_bytes)==doc['sourceSha256'];source=json.loads(gzip.decompress(source_bytes));when=datetime.fromisoformat(doc['frozenAtUTC']).timestamp();verified=datetime.fromisoformat(proof[item['path']]['publiclyVerifiedAtUTC']).timestamp();source_verified=datetime.fromisoformat(doc['sourcePublicationEvidence']['publiclyVerifiedAtUTC']).timestamp();assert source_verified<=when<verified
        assert doc['researchOnly'] and not doc['certifiedStrong'];assert doc['parameters']==config['parameters']==source['parameters'];keys={tuple(k) for k in doc['marketKeys']};assert not seen&keys;seen|=keys;lookup={key(q):q for q in source['models']['residual_point']['forecasts']};winners=[q for q in public_rows(source['models']['stat_point']['selected'],source_verified) if q['kickoff']>when+60 and q['observed']>=when-86400]
        for kind in ('stat','market','residual'):
            expected=[];minimum=.6 if kind=='stat' else .55
            for q in winners:
                params=banks[q['spec'][0]];ref=lookup[key(q)]['marketReference']
                if kind=='stat':p=q
                else:
                    if kind=='market':params=dict(params,point=dict(params['point'],theta=[0.,0.]),replicas=[dict(f,theta=[0.,0.]) for f in params['replicas']])
                    p=adjusted(q,ref,params,minimum)
                if p is not None:expected.append(dict(p,certifiedStrong=False,strategyClass='RAW60_COMPARATOR' if kind=='stat' else 'RESEARCH_EV55'))
            assert expected==doc['models'][kind+'_point']['selected'];lower=[p for q in expected if (p:=conservative(q,minimum)) is not None];assert lower==doc['models'][kind+'_conservative']['selected']
            for mode in ('point','conservative'):
                data=doc['models'][kind+'_'+mode];assert len(data['selected'])==len({(q['fixture'],q['spec'][0]) for q in data['selected']})
                for q in data['forecasts']:assert q['observed']<=when<verified<q['kickoff']
                for q in data['selected']:assert q['p']>=minimum and 1.8<q['odd']<3 and q['p']*q['odd']+q['push']>1 and not q['certifiedStrong'] and 'y' not in q
        cohorts.append(dict(path=item['path'],sha256=item['sha256'],sourceCohort=doc['sourceCohort'],publicationVerified=True,selected={k:len(v['selected']) for k,v in doc['models'].items()}))
    summary=read(out/'summary.json');assert summary['researchOnly'] and not summary['certifiedStrong']
    for name,data in summary['models'].items():
        rows=[r for c in summary['cohorts'] for r in c['models'][name]['selectedRows']];profit=sum(r['y']*r['odd']+r['actualPush']-1 for r in rows);assert data['settled']['bets']==len(rows) and math.isclose(data['settled'].get('profitUnits',0),profit,abs_tol=1e-11)
    write(out/'verification_20261019.json',dict(status='PASS',pinnedState=pinned,boundInputs=len(config['files']),cohorts=cohorts,sourcePublicationChain=True,rawWinnerBeforeCorrection=True,strong60Unchanged=True,researchOnly=True,providerCalls=0,llmCalls=0));print('EV55_SHADOW_VERIFIED',len(cohorts),len(config['files']))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--pinned-state',required=True);a=p.parse_args();main(a.output,a.pinned_state)
