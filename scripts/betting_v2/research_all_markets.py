"""Predeclared all-count revision: full-history scoring and real-price replay.

Three chronological folds, seven separately fitted metrics, 12 weekly refits.
No provider calls, ROI-based arm selection, threshold tuning or LLM calls.
"""
import argparse
from collections import Counter,defaultdict
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.reliable_counts import METRICS,ReliableCounts,metric_values,rolling_predictions
from betting_v2.reliable_goals import CONFIG
from betting_v2.chronological_stack import PriorGoalBank
from betting_v2.validated_walk_forward import FOLDS,distribution,grid
from betting_v2.training_protocol import fit_joint_profile
from betting_v2.research_event_grid import grid_probabilities
from betting_v2.joint_count import outcome
from betting_v2.research_model_revision import read,HISTORY_SHA
from betting_v2.research_csv_prices import write,digest
from betting_v2.live_exact import restore
from betting_v2.live_revised import load_revised
from betting_v2.live_counts import attach_counts
from betting_v2.research_scope_expansion import select_scope
from betting_v2.research_app_markets import prepare_extended,outcome_extended,PERIOD_SCOPES
from betting_v2.research_quotes import assess,stats

ROOT=Path(__file__).resolve().parents[2]


def bound(path):return dict(path=str(path.relative_to(ROOT)),sha256=digest(path.read_bytes()))


def profiles_for(rows):
    result=[fit_joint_profile(rows)]
    for i in range(CONFIG['bootstrapReplicas']):
        result.append(fit_joint_profile([dict(r,means=r['bootstrapMeans'][i]) for r in rows]))
    return result


def score(rows,profile):
    values={}
    for r in rows:
        joint=distribution(r,profile);specs=grid(r['metric']);prob=grid_probabilities(joint,specs)
        losses=[]
        for spec,(p,push) in zip(specs,prob):
            y,ypush=outcome(*r['actual'],spec)
            if not ypush:losses.append((p/(1-push)-y)**2)
        h,a=map(int,r['actual']);mass=joint.pmf[h,a] if h<joint.pmf.shape[0] and a<joint.pmf.shape[1] else 0.
        values[r['fixture']]=dict(brier=float(np.mean(losses)),jointLogLoss=-float(np.log(max(mass,1e-15))),
            meanAbsoluteError=float(np.mean(np.abs(np.asarray(joint.means)-r['actual']))))
    return values


def aggregate(values):
    if not values:return dict(fixtures=0)
    return dict(fixtures=len(values),**{k:float(np.mean([r[k] for r in values])) for k in values[0]})


def baseline_rows(history,fitday,testday,endday,metric):
    directory=ROOT/'reports/betting_v2/oof_research_20261016';stem='oof_ridge_50_'+testday
    state=read(directory/(stem+'_pretest_state.json.gz'))
    before=[r for r in history if r['_date']<direct.timestamp(fitday)]
    samples=dict(metrics={m:sum(direct.metric_values(r,m) is not None for r in before) for m in state['weights'] if m!='period'},
        periodObservations=sum(sum(v>0 for v in direct.metric_values(r,'GOALS_FT')) for r in before if direct.metric_values(r,'GOALS_1H') is not None))
    model=restore(state,[],direct.timestamp(fitday),direct.metric_values,sample_counts=samples)
    rows=[]
    from itertools import groupby
    for day,batch in groupby((r for r in history if direct.timestamp(testday)<=r['_date']<direct.timestamp(endday)),lambda r:int(r['_date']//86400)):
        batch=list(batch)
        for r in batch:
            target=metric_values(r,metric);p=model.predict(r['_home'],r['_away'],metric)
            if target is None or p is None:continue
            rows.append(dict(fixture=f"{r['_date']}:{r['_home']}:{r['_away']}",metric=metric,means=list(p[:2]),actual=list(target),
                regime='XG' if min(len(model.xg[r['_home']]),len(model.xg[r['_away']]))>=8 else 'ELO_NO_XG'))
        for r in batch:model.observe(r,direct.metric_values)
    return rows,read(directory/(stem+'_parameters.json'))


def train(args,history,signature):
    out=args.output;bank=PriorGoalBank(history,metric_values);reports=[]
    for fitday,testday,endday in FOLDS:
        for index,metric in enumerate(METRICS):
            stem=metric+'_'+testday;checkpoint=out/(stem+'_checkpoint.json')
            if checkpoint.exists():
                saved=read(checkpoint)
                if saved['signature']!=signature:raise ValueError('Resume signature mismatch')
                for name,sha in saved['artifactsSha256'].items():
                    if digest((out/name).read_bytes())!=sha:raise ValueError('Changed checkpoint artifact '+name)
                reports.append(saved);print('RESUME_COUNT',stem,flush=True);continue
            write(out/'status.json',dict(status='TRAINING',metric=metric,fold=testday,completed=len(reports),providerCalls=0,llmCalls=0))
            started=time.time();m=ReliableCounts(dict(metric=metric))
            design=m.design(history,direct.timestamp(fitday),metric_values,bank=bank)
            info=m.fit_bank(design,seed=CONFIG['bootstrapSeed']+int(testday.replace('-',''))+index*100)
            print('COUNT_FITTED',stem,design[0].shape,'seconds',round(time.time()-started,1),flush=True)
            calibration=rolling_predictions(m,history,direct.timestamp(fitday),direct.timestamp(testday))
            profiles=profiles_for(calibration)
            paths=[]
            for suffix,value,zipped in [('calibration.json.gz',calibration,True),('pretest_state.json.gz',m.export(),True),('profiles.json',profiles,False)]:
                path=out/(stem+'_'+suffix);write(path,value,zipped=zipped);paths.append(path)
            later=rolling_predictions(m,history,direct.timestamp(testday),direct.timestamp(endday))
            p=out/(stem+'_predictions.json.gz');write(p,later,zipped=True);paths.append(p)
            revised=score(later,profiles[0]);baseline={}
            if metric!='RED_CARDS':
                old,profile=baseline_rows(history,fitday,testday,endday,metric);baseline=score(old,profile)
            common=sorted(set(revised)&set(baseline))
            saved=dict(signature=signature,metric=metric,fitBefore=fitday,testFrom=testday,testBefore=endday,
                training=info,calibrationFixtures=len(calibration),allRevised=aggregate(list(revised.values())),
                pairedRevised=aggregate([revised[k] for k in common]),pairedOld=aggregate([baseline[k] for k in common]),
                pairedLosses=[dict(fixture=k,revised=revised[k],old=baseline[k]) for k in common],
                additionalRevisedFixtures=len(set(revised)-set(baseline)),missingRevisedFixtures=len(set(baseline)-set(revised)),
                artifactsSha256={p.name:digest(p.read_bytes()) for p in paths},seconds=round(time.time()-started,2))
            write(checkpoint,saved);reports.append(saved)
            print('COUNT_SCORED',stem,'new',saved['allRevised'],'old',saved['pairedOld'],flush=True)
    return reports


def deployment(out):
    entries={}
    for metric in METRICS:
        stem=metric+'_2026-10-01'
        entries[metric]=dict(state=bound(out/(stem+'_pretest_state.json.gz')),profiles=bound(out/(stem+'_profiles.json')))
    return dict(schema=1,variant='all_market_reliability_recency_uncertainty',fittedBefore='2026-09-01',calibrationBefore='2026-10-01',
        metrics=entries,lowerQuantile=.1,historySha256=HISTORY_SHA,profitabilityValidated=False,empiricalTiersValidated=False,
        cardContracts=dict(YELLOW_CARDS='observed yellow counts',CARDS='observed yellow plus red; diagnostic only',RED_CARDS='observed red counts'),
        priceCalibration='No prior selected-price calibrator fitted for counts; historical statistical likelihood profiles only')


def replay(args,manifest):
    # Historical feature states are immutable. Only post-October observations
    # are rolled from current cached actuals, identically for both model arms.
    direct.ROOT=ROOT;history=direct.load_actual_results(direct.timestamp('2026-10-16'))['GLOBAL']
    truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};reports={}
    for packet in ('earliest','latest'):
        path=ROOT/'reports/betting_v2/scope_expansion_v2_20261016'/('quotes_'+packet+'.json.gz')
        quotes=read(path);prices=defaultdict(dict);options={a:[] for a in ('current','all_market_point','all_market_decision')};missing=Counter()
        for q in quotes:
            if tuple(q['spec'][:2])==('GOALS_FT','RESULT'):prices[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
        for cutoff in sorted({q['featureCutoff'] for q in quotes}):
            current,profile,_=load_revised(ROOT,history,cutoff,include_counts=False)
            new,newprofile,_=attach_counts(ROOT,history,cutoff,current,profile,{},manifest)
            cache={}
            for q in quotes:
                if q['featureCutoff']!=cutoff:continue
                raw=truth.get(q['fixture']);metric=q['spec'][0]
                if raw is None:missing['noExactActualFixture']+=1;continue
                actual=metric_values(raw,metric)
                if actual is None:missing['noActual::'+metric]+=1;continue
                first=metric_values(raw,'GOALS_1H')
                if q['spec'][1] in PERIOD_SCOPES and first is None:missing['noHalfActual']+=1;continue
                triplet=prices[q['fixture'],q['bookmaker'],q['sourceReceipt']];fav=None
                if set(triplet)=={'HOME','DRAW','AWAY'}:
                    side=min(triplet,key=triplet.get)
                    if side!='DRAW' and triplet[side]<=1.8:fav=side
                for arm,model,p in [('current',current,profile),('all_market_point',new,newprofile)]:
                    key=arm,q['_home'],q['_away'],metric
                    if key not in cache:
                        pred=model.predict(q['_home'],q['_away'],metric);full=model.predict(q['_home'],q['_away'],'GOALS_FT')
                        if pred is None or full is None:cache[key]=None
                        else:
                            regime='XG' if min(len(model.xg[q['_home']]),len(model.xg[q['_away']]))>=8 else 'ELO_NO_XG'
                            row=dict(metric=metric,regime=regime,means=list(pred[:2]))
                            if metric in ('GOALS_1H','GOALS_2H'):row.update(fullMeans=list(full[:2]),shares=[pred[i]/full[i] for i in (0,1)])
                            if metric=='GOALS_FT':
                                half=model.predict(q['_home'],q['_away'],'GOALS_1H')
                                if half is not None:row['periodShares']=[half[i]/full[i] for i in (0,1)]
                            cache[key]=row,prepare_extended(row,p),distribution(dict(metric='GOALS_FT',regime=regime,means=list(full[:2])),p)
                    prediction=cache[key]
                    if prediction is None:missing[arm+'::noPrediction::'+metric]+=1;continue
                    if q['spec'][1] in PERIOD_SCOPES and 'periodShares' not in prediction[0]:continue
                    pick=assess(q,prediction,p,fav,'current_preference')
                    if pick is None:continue
                    y,push=outcome_extended(*actual,q['spec'],first=first);pick=dict(pick,y=y,actualPush=push)
                    options[arm].append(pick)
                    if arm=='all_market_point':
                        # Existing goal decisions keep their established bounds;
                        # every newly fitted count bank receives its own bounds.
                        if metric not in METRICS and q['spec'][1] not in ('TOTAL','TEAM','RESULT','DC','DNB','BTTS'):
                            missing['extendedGoalDecisionBoundsUnavailable']+=1;continue
                        b=model.bounds(q['_home'],q['_away'],q['spec'],q['odd'])
                        if b is None:continue
                        attack=metric.startswith('GOALS') or metric in ('CORNERS','SHOTS','SOT')
                        support=b[fav.lower()] if fav else None
                        if b['win']<.6 or b['ev']<=0 or (fav and attack and (support is None or support<.6)):continue
                        confidence=min(b['win'],support) if support is not None else b['win']
                        options['all_market_decision'].append(dict(pick,pointP=pick['p'],p=b['win'],push=b['push'],ev=b['ev'],confidence=confidence,
                            tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING'))
        arms={}
        for arm,rows in options.items():
            picks=select_scope(rows)
            arms[arm]=dict(overall=stats(picks),byMetric={m:stats([r for r in picks if r['spec'][0]==m]) for m in sorted({r['spec'][0] for r in quotes})},picks=picks)
        report=dict(packet=packet,quoteSha256=digest(path.read_bytes()),quotes=len(quotes),fixtures=len({r['fixture'] for r in quotes}),
            arms=arms,missing=dict(missing),untouchedHoldout=False,profitabilityValidated=False)
        write(args.output/('priced_'+packet+'.json'),report)
        reports[packet]={k:{a:b for a,b in v.items() if a!='picks'} for k,v in arms.items()}
        print('COUNT_PRICED',packet,json.dumps(reports[packet]),flush=True)
    return reports


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--data-root',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=True)
    try:
        direct.ROOT=args.data_root;history=direct.load_actual_results(direct.timestamp('2026-10-15'))['GLOBAL']
        sha=digest(json.dumps(history,sort_keys=True,separators=(',',':')).encode())
        if sha!=HISTORY_SHA:raise ValueError('Pinned history mismatch '+sha)
        sources=[Path(__file__),Path(__file__).with_name('reliable_counts.py'),Path(__file__).with_name('reliable_goals.py'),Path(__file__).with_name('chronological_stack.py')]
        signature=digest((sha+''.join(digest(p.read_bytes()) for p in sources)+json.dumps(CONFIG,sort_keys=True)).encode())
        inventory=dict(signature=signature,historySha256=sha,historyFixtures=len(history),
            availableLabels={m:sum(metric_values(r,m) is not None for r in history) for m in METRICS},folds=FOLDS,
            config=CONFIG,metrics=list(METRICS),coreFits=3*len(METRICS)*(CONFIG['bootstrapReplicas']+1),
            providerCalls=0,llmCalls=0,untouchedHoldout=False,roiOptimized=False)
        write(args.output/'inventory.json',inventory)
        reports=train(args,history,signature);manifest=deployment(args.output)
        metrics={}
        for metric in METRICS:
            selected=[r for r in reports if r['metric']==metric];paired=[p for r in selected for p in r['pairedLosses']]
            metrics[metric]=dict(testPredictions=sum(r['allRevised']['fixtures'] for r in selected),
                pairedRevised=aggregate([r['revised'] for r in paired]),pairedOld=aggregate([r['old'] for r in paired]),
                folds=[{k:v for k,v in r.items() if k not in ('pairedLosses','artifactsSha256','training')} for r in selected])
        # Do not activate this manifest before research and publication checks.
        write(args.output/'candidate_deployment.json',manifest)
        write(args.output/'status.json',dict(status='REPLAYING',coreFits=inventory['coreFits'],providerCalls=0,llmCalls=0))
        priced=replay(args,manifest)
        summary=dict(status='COMPLETE',inventory=inventory,metrics=metrics,priced=priced,providerCalls=0,llmCalls=0,
            profitabilityValidated=False,selectedProfitableWinner=False,
            limitations=['Previously inspected development folds; no untouched holdout',
                'Weekly parameter/profile sensitivity does not include all model misspecification',
                'Count price history begins in October, so no count selected-price calibrator is fitted on later labels',
                'Yellow, red and yellow+red diagnostics are separate; bookmaker card weights and second yellows require settlement-specific labels',
                'Earliest and latest packets overlap and are never pooled',
                'Conditional count dominance is not a learned causal relationship with the match winner'])
        write(args.output/'summary.json',summary);write(args.output/'status.json',dict(status='COMPLETE',coreFits=inventory['coreFits'],providerCalls=0,llmCalls=0))
    except Exception as exc:
        write(args.output/'status.json',dict(status='FAILED',errorType=type(exc).__name__,error=str(exc),providerCalls=0,llmCalls=0));raise

if __name__=='__main__':main()
