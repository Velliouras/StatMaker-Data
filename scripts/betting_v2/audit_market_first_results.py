"""Independent checks for wider count receipts and their fixed decision reports."""
import argparse,math,sys
from pathlib import Path
from collections import defaultdict
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import ROOT,PRIOR,OCTOBER
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.reliable_counts import metric_values
from betting_v2.research_git_archive import absolute_time
from betting_v2.research_scope_expansion import select_scope
from betting_v2.research_context_market import conservative_winner
from collections import Counter

def main(out):
    manifest=read(out/'input_bindings.json');assert all(digest((ROOT/p).read_bytes())==h for p,h in manifest['files'].items())
    history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};results={}
    for packet in ('earliest','latest'):
        rows=read(out/f'quotes_{packet}.json.gz');groups=defaultdict(list)
        for q in rows:
            assert str(q['providerEventId']).isdigit();updated=absolute_time(q['marketUpdatedAtUTC']);assert updated<=q['observed']<q['kickoff'] and q['observed']-updated<=86400
            assert q['featureCutoff']%86400==0 and q['featureCutoff']<=q['observed']-86400
            evidence=q['_sameReceiptResultOdds'];fav=None
            if set(evidence)=={'HOME','DRAW','AWAY'}:
                side=min(evidence,key=evidence.get)
                if side!='DRAW' and evidence[side]<=1.8:fav=side
            assert fav==q['_favourite'];groups[q['fixture'],q['spec'][0]].append(q)
        for key,values in groups.items():assert len({(q['sourceReceipt'],q['observed'],q['featureCutoff'],q['providerEventId'],q['kickoff']) for q in values})==1
        index={(q['fixture'],q['spec'][0],q['bookmaker'],tuple(q['spec']),q['sourceReceipt'],q['odd']) for q in rows};doc=read(out/f'priced_{packet}.json');reports={}
        checkpoint=read(out/f'replay_checkpoint_{packet}.json.gz');options=checkpoint['options'];options['coupled_sot_matched']=[q for q in options['coupled'] if q['spec'][0]=='SOT' and q['fixture'] in set(checkpoint['matchedSotFixtures'])]
        for name,report in doc['reports'].items():
            picks=report['picks'];assert len({(r['fixture'],r['spec'][0]) for r in picks})==len(picks);profit=0.;wins=pushes=0
            for r in picks:
                assert (r['fixture'],r['spec'][0],r['bookmaker'],tuple(r['spec']),r['sourceReceipt'],r['odd']) in index
                assert r['p']>=.6 and 1.8<r['odd']<3 and r['p']*r['odd']+r['push']>1
                assert 0<=r['push'] and r['p']+r['push']<=1+1e-12
                if name.endswith('_conservative'):
                    lower=r['_lower'];assert r['p']<=lower['p']+1e-12 and r['push']<=lower['push']+1e-12
                    if r['spec'][0] in ('CORNERS','SHOTS','SOT') and lower['support'] is not None:assert lower['support']>=.6
                h,a=metric_values(truth[r['fixture']],r['spec'][0]);metric,scope,side,team,line=r['spec'];count=h if team=='HOME' else a if team=='AWAY' else h+a
                ap=float(count==line);y=float(count>line if side=='OVER' else count<line);assert r['actualPush']==ap and r['y']==y
                profit+=y*r['odd']+ap-1;wins+=y;pushes+=ap
            if name.endswith('_conservative'):
                point=doc['reports'][name.removesuffix('_conservative')+'_point']['picks'];keys={(r['fixture'],tuple(r['spec']),r['bookmaker'],r['sourceReceipt']) for r in point};assert all((r['fixture'],tuple(r['spec']),r['bookmaker'],r['sourceReceipt']) in keys for r in picks)
            summary=report['overall'];assert summary['bets']==len(picks)
            if picks:assert abs(profit-summary['profitUnits'])<1e-10 and abs(profit/len(picks)-summary['roi'])<1e-12 and wins==summary['wins'] and pushes==summary['pushes']
            kind,mode=name.rsplit('_',1);selected=select_scope(options[kind])
            if mode=='conservative':selected=[p for w in selected if (p:=conservative_winner(w)) is not None]
            pending=[r for r in selected if r['fixture'] not in truth or metric_values(truth[r['fixture']],r['spec'][0]) is None]
            assert dict(Counter(r['spec'][0] for r in pending))==report['missingActual']
            assert len(selected)==len(picks)+len(pending)
            low=profit-len(pending);high=profit+sum(r['odd']-1 for r in pending)
            reports[name]=dict(bets=len(picks),profitUnits=profit,selectedIncludingUnknown=len(selected),unknownOutcomes=len(pending),allSelectedProfitBounds=[low,high],allSelectedRoiBounds=[low/len(selected),high/len(selected)] if selected else None,verified=True)
        results[packet]=dict(marketPackets=len(groups),quoteRows=len(rows),reports=reports)
    write(out/'verification.json',dict(status='PASS',boundInputs=len(manifest['files']),results=results,providerCalls=0,llmCalls=0));print('MARKET_FIRST_RESULTS_VERIFIED',results)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
