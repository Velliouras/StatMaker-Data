"""Count-market-first archive packets, fixed monthly models and unchanged gates."""
import argparse,sys
from pathlib import Path
from collections import defaultdict,Counter
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_probability_recalibration import ROOT,PRIOR,OCTOBER,FOLDS,MonthlyModels
from betting_v2.research_git_archive import load_snapshot,extract_snapshot,remove_conflicts,settle_selected,COUNT_METRICS
from betting_v2.audit_raw_contract_archives import provider_bindings,join
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.research_scope_expansion import select_scope
from betting_v2.research_context_market import eligible,conservative_winner
from betting_v2.research_cross_market import prepared_law,law_value
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.audit_probability_trial import canonical_summary
from betting_v2.conditional_sot import ConversionHistory,distribution as sot_distribution

KINDS=('control','coupled')

def verify(out):
    m=read(out/'input_bindings.json');bad=[p for p,h in m['files'].items() if digest((ROOT/p).read_bytes())!=h]
    if bad:raise ValueError('Changed market-first inputs '+str(bad))
    return digest((out/'input_bindings.json').read_bytes())

def retain(chosen,rows,latest=False):
    grouped=defaultdict(list)
    for r in rows:
        if r['spec'][0] in COUNT_METRICS and r['spec'][1] in ('TOTAL','TEAM'):grouped[r['fixture'],r['spec'][0]].append(r)
    for key,values in grouped.items():
        clocks={(r['observed'],r['sourceReceipt'],r['featureCutoff'],r['kickoff'],r['providerEventId']) for r in values}
        if len(clocks)!=1:raise ValueError('Market packet mixed clocks')
        rank=values[0]['observed'],values[0]['sourceReceipt']
        if key not in chosen or (rank>tuple(chosen[key]['rank']) if latest else rank<tuple(chosen[key]['rank'])):chosen[key]=dict(rank=list(rank),rows=values)

def collect(out,cache,signature):
    path=out/'collection_checkpoint.json.gz';saved=read(path) if path.exists() else dict(signature=signature,done=[],earliest=[],latest=[],sources=[])
    if saved['signature']!=signature:raise ValueError('Collection checkpoint mismatch')
    chosen={p:{tuple(v['key']):v['value'] for v in saved[p]} for p in ('earliest','latest')};done=set(saved['done']);sources=saved['sources'];bridge=provider_bindings();cache.mkdir(parents=True,exist_ok=True);jobs=[]
    for source in (PRIOR,OCTOBER):
        m=read(source/'manifest.json');jobs.extend((s,m) for s in m['snapshots'] if s['sha'] not in done)
    def fetch(job):return load_snapshot(job[0],job[1],cache)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for binding,doc in pool.map(fetch,jobs):
            rows,_=extract_snapshot(doc,binding);rows,_=remove_conflicts(rows);rows=[q for q in rows if str(q['providerEventId']).isdigit()];rows,_=join(rows,bridge);fav=fixture_favourite(rows)
            result_odds=defaultdict(dict)
            for q in rows:
                if q['spec'][:2]==('GOALS_FT','RESULT') or q['spec'][:2]==['GOALS_FT','RESULT']:result_odds[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
            counts=[dict(q,_favourite=fav.get((q['fixture'],q['bookmaker'],q['sourceReceipt'])),_sameReceiptResultOdds=result_odds[q['fixture'],q['bookmaker'],q['sourceReceipt']]) for q in rows if q['spec'][0] in COUNT_METRICS and q['spec'][1] in ('TOTAL','TEAM')]
            for p in chosen:retain(chosen[p],counts,p=='latest')
            done.add(binding['sha']);sources.append(binding)
            write(path,dict(signature=signature,done=sorted(done),sources=sources,**{p:[dict(key=list(k),value=v) for k,v in data.items()] for p,data in chosen.items()}),zipped=True)
            write(out/'progress.json',dict(stage='COLLECT',snapshots=len(done),expectedSnapshots=85,marketPackets={p:len(v) for p,v in chosen.items()},providerCalls=0,llmCalls=0));print('MARKET_FIRST_SOURCE',len(done),{p:len(v) for p,v in chosen.items()},flush=True)
            del doc
    coverage={}
    for p,data in chosen.items():
        rows=[q for v in data.values() for q in v['rows']];write(out/f'quotes_{p}.json.gz',rows,zipped=True)
        original=read(PRIOR/f'quotes_{p}.json.gz')+read(OCTOBER/f'quotes_{p}.json.gz')
        coverage[p]={m:dict(quotes=sum(q['spec'][0]==m for q in rows),fixtures=len({q['fixture'] for q in rows if q['spec'][0]==m}),originalWholeFixturePacketFixtures=len({q['fixture'] for q in original if q['spec'][0]==m and q['spec'][1] in ('TOTAL','TEAM')})) for m in COUNT_METRICS}
    write(out/'coverage.json',dict(status='COMPLETE',snapshotsVerified=len(done),archivedBytesVerified=sum(x['bytes'] for x in sources),packets=coverage,inputSignature=signature,providerCalls=0,llmCalls=0))

def probability_scores(rows):
    if not rows:return dict(events=0,fixtures=0)
    counts=Counter(r['fixture'] for r in rows);w=np.asarray([1/counts[r['fixture']] for r in rows]);w/=w.sum();target=np.asarray([[r['y'],r['push'],1-r['y']-r['push']] for r in rows]);result=dict(events=len(rows),fixtures=len(counts))
    for kind in KINDS:
        a=np.asarray([r['probabilities'][kind] for r in rows]);a=np.c_[a,1-a.sum(axis=1)]
        if not np.isfinite(a).all() or np.any(a < -1e-9):raise ValueError('Invalid expanded count probabilities')
        result[kind]=dict(logLoss=float(w@(-(target*np.log(np.clip(a,1e-12,1))).sum(axis=1))),brier=float(w@((a-target)**2).sum(axis=1)))
    return result

def replay(out,signature):
    history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    profiles={d:{m:read(PRIOR/f'{m}_{d}_coupled_profiles.json')['profiles'] if d!=FOLDS[-1] else read(ROOT/f'reports/betting_v2/combined_candidate_20261016/{m}_profiles.json') for m in COUNT_METRICS} for d in FOLDS}
    sot=read(ROOT/'reports/betting_v2/conditional_sot_20261018/parameter_banks.json')['fits'];results={}
    for packet in ('earliest','latest'):
        quotes=read(out/f'quotes_{packet}.json.gz');byday=defaultdict(list)
        for q in quotes:byday[q['featureCutoff']].append(q)
        checkpoint=out/f'replay_checkpoint_{packet}.json.gz';saved=read(checkpoint) if checkpoint.exists() else dict(signature=signature,done=[],options={},proper=[],diagnostics={},matchedSotFixtures=[])
        if saved['signature']!=signature:raise ValueError('Replay checkpoint mismatch')
        done=set(saved['done']);options=defaultdict(list,saved['options']);proper=saved['proper'];diagnostics=Counter(saved['diagnostics']);models=MonthlyModels(history,profiles);conversion=ConversionHistory(history,direct.metric_values);matched_sot=set(saved.get('matchedSotFixtures',[]))
        for cutoff,rows in sorted(byday.items()):
            if cutoff in done:continue
            models.advance(cutoff);conversion.advance(cutoff);seen=set();new_sot={}
            for q in rows:
                metric=q['spec'][0];fav=q['_favourite'];values={};preds={};key=q['fixture'],tuple(q['spec']),q['bookmaker'],q['sourceReceipt']
                for kind in KINDS:
                    pred=models.prediction(q['_home'],q['_away'],metric,kind);preds[kind]=pred
                    if pred is None:diagnostics['missingPrediction::'+metric]+=1;continue
                    v=forecast(pred['rows'][0],pred['profiles'][0],q['spec'],pred['prepared'][0]);values[kind]=v
                    candidate=eligible(q,v,pred['full'],fav,v['winProbability'],v['pushProbability'])
                    if candidate is not None:
                        replicas=[forecast(r,p,q['spec'],prep) for r,p,prep in zip(pred['rows'][1:],pred['profiles'][1:],pred['prepared'][1:])]
                        support=None if not fav else min(v['scenarios'][fav]['winProbability'] or 0.,float(np.quantile([x['scenarios'][fav]['winProbability'] or 0. for x in replicas],.1)))
                        candidate['_lower']=dict(p=min(v['winProbability'],float(np.quantile([x['winProbability'] for x in replicas],.1))),push=min(v['pushProbability'],float(np.quantile([x['pushProbability'] for x in replicas],.1))),support=support);options[kind].append(candidate)
                if metric=='SOT' and preds.get('coupled') is not None:
                    if q['fixture'] not in new_sot:
                        parent=models.prediction(q['_home'],q['_away'],'SHOTS','coupled');feature=conversion.features(q['_home'],q['_away'])
                        if parent is not None:
                            def unwrap(j):return j.joint if hasattr(j,'joint') else j
                            preps=[prepared_law(sot_distribution(unwrap(parent['prepared'][0][0]),feature,sot[models.fold]['point']))]+[prepared_law(sot_distribution(unwrap(p[0]),feature,m)) for p,m in zip(parent['prepared'][1:],sot[models.fold]['replicas'])]
                            new_sot[q['fixture']]=preps;matched_sot.add(q['fixture'])
                    if q['fixture'] in new_sot:
                        v,*replicas=[law_value(p,q['spec']) for p in new_sot[q['fixture']]];candidate=eligible(q,v,preds['coupled']['full'],fav,v['winProbability'],v['pushProbability'])
                        if candidate is not None:
                            support=None if not fav else min(v['scenarios'][fav]['winProbability'] or 0.,float(np.quantile([x['scenarios'][fav]['winProbability'] or 0. for x in replicas],.1)))
                            candidate['_lower']=dict(p=min(v['winProbability'],float(np.quantile([x['winProbability'] for x in replicas],.1))),push=min(v['pushProbability'],float(np.quantile([x['pushProbability'] for x in replicas],.1))),support=support);options['conditional_sot'].append(candidate)
                actual=direct.metric_values(truth[q['fixture']],metric) if q['fixture'] in truth else None
                canonical=q['fixture'],metric,q['spec'][1],q['spec'][3],q['spec'][4],q['bookmaker'],q['sourceReceipt']
                if actual is not None and len(values)==len(KINDS) and canonical not in seen:
                    y,ap=outcome(*actual,q['spec']);probs={k:[v['winProbability'],v['pushProbability']] for k,v in values.items()}
                    if q['spec'][2]=='UNDER':y=1-y-ap;probs={k:[1-v[0]-v[1],v[1]] for k,v in probs.items()}
                    proper.append(dict(fixture=q['fixture'],fold=models.fold,metric=metric,y=y,push=ap,probabilities=probs));seen.add(canonical)
            done.add(cutoff);write(checkpoint,dict(signature=signature,done=sorted(done),options=dict(options),proper=proper,diagnostics=dict(diagnostics),matchedSotFixtures=sorted(matched_sot)),zipped=True);write(out/'progress.json',dict(stage='REPLAY',packet=packet,completedDays=len(done),totalDays=len(byday),providerCalls=0,llmCalls=0));print('MARKET_FIRST_DAY',packet,cutoff,{k:len(v) for k,v in options.items()},flush=True)
        reports={}
        options['coupled_sot_matched']=[q for q in options['coupled'] if q['spec'][0]=='SOT' and q['fixture'] in matched_sot]
        for kind in (*KINDS,'conditional_sot','coupled_sot_matched'):
            winners=select_scope(options[kind])
            for mode,picks in [('point',winners),('conservative',[p for w in winners if (p:=conservative_winner(w)) is not None])]:
                settled,missing=settle_selected(picks,truth);reports[kind+'_'+mode]=dict(overall=canonical_summary(settled),missingActual=missing,picks=settled,byMetric={m:canonical_summary([r for r in settled if r['spec'][0]==m]) for m in COUNT_METRICS},byMonth={d:canonical_summary([r for r in settled if datetime.fromtimestamp(r['kickoff'],timezone.utc).strftime('%Y-%m')==d[:7]]) for d in FOLDS})
        scores={m:{d:probability_scores([r for r in proper if r['metric']==m and r['fold']==d]) for d in FOLDS} for m in COUNT_METRICS};write(out/f'priced_{packet}.json',dict(reports=reports,probabilityScores=scores,diagnostics=dict(diagnostics)))
        results[packet]=dict(reports={k:{a:b for a,b in v.items() if a!='picks'} for k,v in reports.items()},probabilityScores=scores,diagnostics=dict(diagnostics))
    write(out/'summary.json',dict(status='COMPLETE',results=results,primary='coupled_conservative',secondary='conditional_sot_conservative',providerCalls=0,llmCalls=0,modelFits=0,thresholdsChanged=False,roiOptimized=False,untouchedHoldout=False,deployed=False,inputSignature=signature))

# Exact settlement remains independent from model forecasts.
from betting_v2.research_app_markets import outcome_extended as outcome
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--stage',choices=['collect','replay','all'],default='all');p.add_argument('--cache',type=Path,default=Path('/tmp/statmaker-market-first-cache'));a=p.parse_args();signature=verify(a.output)
    if a.stage in ('collect','all'):collect(a.output,a.cache,signature)
    if a.stage in ('replay','all'):replay(a.output,signature)
