"""Post-hoc threshold sensitivity on frozen raw winners; no active rule change."""
import sys,argparse,copy
from pathlib import Path
from collections import defaultdict
from datetime import datetime,timezone
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import ROOT,PRIOR,FOLDS
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_git_archive import absolute_time,settle_selected
from betting_v2.research_scope_expansion import select_scope
from betting_v2.audit_probability_trial import canonical_summary
from betting_v2.market_residual import predict,candidate
from betting_v2.market_calibration import references
from betting_v2.research_expanded_calibration import prices
SOURCE=ROOT/'reports/betting_v2/market_first_counts_20261018'
BANKS=ROOT/'reports/betting_v2/market_residual_20261019/parameter_banks.json'


def adjusted(q,reference,parameters,minimum):
    side=q['spec'][2];raw=q['p']/(1-q['push']);stat=raw if side=='OVER' else 1-raw;market=reference['q'] if side=='OVER' else 1-reference['q'];theta=parameters['point']['theta'];prob=predict(stat,market,theta);win=(prob if side=='OVER' else 1-prob)*(1-q['push']);p=min(q['p'],win);ev=p*q['odd']+q['push']-1
    if p<minimum or not 1.8<q['odd']<3 or ev<=0:return None
    lower=q['_lower'];low=lower['p']/(1-q['push']);ls=low if side=='OVER' else 1-low;mapped=[]
    for f in parameters['replicas']:
        v=predict(ls,market,f['theta']);mapped.append((v if side=='OVER' else 1-v)*(1-q['push']))
    v=predict(ls,market,theta);mapped_point=(v if side=='OVER' else 1-v)*(1-q['push']);confidence=min(q['confidence'],p)
    return dict(q,p=p,ev=ev,confidence=confidence,tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING',_lower=dict(lower,p=min(lower['p'],p,mapped_point,float(np.quantile(mapped,.1)))),marketReference=reference,residualWinProbability=win,uncalibratedPointP=q['p'])


def conservative(q,minimum):
    low=q['_lower'];p=min(q['p'],low['p']);push=min(q['push'],low['push']);ev=p*q['odd']+push-1
    if p<minimum or ev<=0:return None
    if q['spec'][0] in ('CORNERS','SHOTS','SOT') and low['support'] is not None and low['support']<.6:return None
    confidence=min(p,low['support']) if low['support'] is not None else p
    return dict(q,p=p,push=push,ev=ev,confidence=confidence,tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING')


def key(q):return q['fixture'],q['bookmaker'],tuple(q['spec']),q['sourceReceipt'],q['odd']


def main(out):
    out.mkdir(parents=True,exist_ok=True);banks=read(BANKS)['fits'];history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};results={};first={};parity=0
    for packet in ('earliest','latest'):
        quotes=read(SOURCE/f'quotes_{packet}.json.gz');refs,_=references(quotes);lookup={key(q):refs[i] for i,q in enumerate(quotes) if i in refs};saved=read(SOURCE/f'replay_checkpoint_{packet}.json.gz');available=[]
        for row in saved['options']['coupled']:
            valid=[d for d in FOLDS[1:] if absolute_time(d+'T00:00:00Z')<=row['featureCutoff']]
            if valid and key(row) in lookup and banks[packet][max(valid)][row['spec'][0]]['point']['fitted']:available.append(row)
        winners=select_scope(available);selected=defaultdict(list)
        for q in winners:
            valid=[d for d in FOLDS[1:] if absolute_time(d+'T00:00:00Z')<=q['featureCutoff']];ref=lookup.get(key(q))
            if not valid or ref is None:continue
            parameters=banks[packet][max(valid)][q['spec'][0]]
            if not parameters['point']['fitted']:continue
            assert adjusted(q,ref,parameters,.6)==candidate(q,ref,parameters);parity+=1
            for minimum in (.6,.55):
                point=adjusted(q,ref,parameters,minimum)
                if point is None:continue
                name='fixed_winner_'+str(int(minimum*100));selected[name+'_point'].append(point);lower=conservative(point,minimum)
                if lower is not None:selected[name+'_conservative'].append(lower)
        reports={}
        for minimum in ('60','55'):
            for mode in ('point','conservative'):
                name='fixed_winner_'+minimum+'_'+mode;picks=selected[name];graded,missing=settle_selected(picks,truth);reports[name]=dict(overall=canonical_summary(graded),selectedBeforeSettlement=len(picks),missingActual=missing,byMonth={fold:canonical_summary([r for r in graded if max(d for d in FOLDS if absolute_time(d+'T00:00:00Z')<=r['featureCutoff'])==fold]) for fold in FOLDS[1:]},picks=graded)
        results[packet]={k:{a:b for a,b in r.items() if a!='picks'} for k,r in reports.items()};write(out/f'priced_{packet}.json.gz',dict(reports=reports,selected=dict(selected)),zipped=True)
        if packet=='earliest':first=dict(selected)
    latest=read(SOURCE/'quotes_latest.json.gz');price={k:prices(v,latest) for k,v in first.items()};write(out/'fixed_first_prices.json.gz',price,zipped=True)
    # These are pending projections, not certified STRONG or a deployed policy.
    folder=ROOT/'reports/betting_v2/forward_market_residual_20261019';index=read(folder/'index.json');current=[];now=datetime.now(timezone.utc).timestamp()
    for c in index['cohorts']:
        if digest((folder/c['path']).read_bytes())!=c['sha256']:raise ValueError('Immutable source cohort changed')
        d=read(folder/c['path']);f={key(q):q for q in d['models']['residual_point']['forecasts']};pending=[]
        for q in d['models']['stat_point']['selected']:
            if q['kickoff']<=now:continue
            ref=f[key(q)]['marketReference'];parameters=banks['earliest']['2026-10-19'][q['spec'][0]];p=adjusted(q,ref,parameters,.55)
            if p is None:continue
            p=dict(p,strategyClass='RESEARCH_EV55',certifiedStrong=False);pending.append(p)
        current.append(dict(sourceCohort=c['path'],sourceSha256=c['sha256'],projections=pending,conservativeProjections=[p for q in pending if (p:=conservative(q,.55)) is not None]))
    inputs=[BANKS,Path(__file__),*[SOURCE/f'{prefix}_{packet}.json.gz' for prefix in ('quotes','replay_checkpoint') for packet in ('earliest','latest')]]
    write(out/'current_ev55_research_projections.json',dict(createdAtUTC=datetime.now(timezone.utc).isoformat(),researchOnly=True,notCertifiedStrong=True,activePolicyChanged=False,cohorts=current))
    summary=dict(status='COMPLETE',postHocPolicySensitivity=True,thresholdsCompared=[.6,.55],primaryStrongThresholdChanged=False,rawWinnerFrozenBeforeAdjustment=True,runnerUpFallback=False,results=results,priceAudit={k:{a:b for a,b in v.items() if a!='rows'} for k,v in price.items()},parity60Offers=parity,inputsSha256={str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in inputs},providerCalls=0,llmCalls=0,modelFits=0,deployed=False)
    write(out/'summary.json',summary);print('EV55_SENSITIVITY',{packet:{k:(v['overall']['bets'],v['overall'].get('roi')) for k,v in r.items()} for packet,r in results.items()},'CURRENT',[(len(c['projections']),len(c['conservativeProjections'])) for c in current])
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
