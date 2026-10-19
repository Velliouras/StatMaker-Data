"""Independent frozen-packet, timing, gate and published payout reconciliation.
Does not requery upstream outcomes or recreate model fits.
"""
import argparse,gzip,json,math,sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_csv_prices import digest,write
from betting_v2.research_context_market import conservative_winner

def main(out,pinned):
    index=json.loads((out/'index.json').read_text());proof=json.loads((out/'publication_evidence.json').read_text());seen=set();summary=json.loads((out/'summary.json').read_text());checked={}
    for item in index['cohorts']:
        raw=(out/item['path']).read_bytes();assert digest(raw)==item['sha256']==proof[item['path']]['sha256'];doc=json.loads(gzip.decompress(raw));keys={tuple(k) for k in doc['marketKeys']};assert len(keys)==len(doc['marketKeys']) and not seen&keys;seen|=keys
        frozen=datetime.fromisoformat(doc['frozenAtUTC']).timestamp();verified=datetime.fromisoformat(proof[item['path']]['publiclyVerifiedAtUTC']).timestamp();assert frozen<verified
        for name,arm in doc['models'].items():
            forecasts=arm['forecasts'];lookup={(q['fixture'],tuple(q['spec']),q['bookmaker'],q['sourceReceipt'],q['odd']) for q in forecasts}
            for q in forecasts:
                assert q['observed']<=frozen<verified<q['kickoff'];assert (q['fixture'],q['spec'][0]) in keys;assert q['featureCutoff']%86400==0 and q['featureCutoff']<=q['observed']-86400
                assert 0<=q['observed']-datetime.fromisoformat(q['marketUpdatedAtUTC']).timestamp()<=86400
            assert len({(q['fixture'],q['spec'][0]) for q in arm['selected']})==len(arm['selected'])
            for q in arm['selected']:
                assert q['p']>=.60 and 1.80<q['odd']<3 and q['ev']>0
                assert (q['fixture'],tuple(q['spec']),q['bookmaker'],q['sourceReceipt'],q['odd']) in lookup
            if name.endswith('_conservative'):
                points=doc['models'][name.removesuffix('_conservative')+'_point']['selected'];expected=[r for w in points if (r:=conservative_winner(w)) is not None]
                assert expected==arm['selected']
        checked[item['path']]=dict(sha256=item['sha256'],publicationVerified=True,marketPackets=len(keys),models={n:len(a['selected']) for n,a in doc['models'].items()})
    assert seen=={tuple(k) for k in index['marketKeys']}
    for name,model in summary['models'].items():
        rows=[r for c in summary['cohorts'] for r in c['models'][name]['selectedRows']];profit=sum(r['y']*r['odd']+r['actualPush']-1 for r in rows)
        assert model['settled']['bets']==len(rows) and math.isclose(profit,model['settled'].get('profitUnits',0),abs_tol=1e-12)
        assert model['pendingEligible']==sum(c['models'][name]['pendingEligible'] for c in summary['cohorts'])
    root=Path(__file__).resolve().parents[2];config=json.loads((out/'candidate.json').read_text());bad=[q for q,h in config['files'].items() if not (root/q).exists() or digest((root/q).read_bytes())!=h];assert not bad,bad
    result=dict(status='PASS',asOfUTC=summary['generatedAtUTC'],pinnedState=pinned,sourceInputs=len(config['files']),marketPackets=len(seen),cohorts=checked,payoutArithmetic=True,sourceOutcomeRequery=False,providerCalls=0,llmCalls=0)
    write(out/'verification_20261019.json',result);print('FORWARD_VERIFICATION_PASS',len(checked),len(seen),len(config['files']))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--pinned-state',required=True);a=p.parse_args();main(a.output,a.pinned_state)
