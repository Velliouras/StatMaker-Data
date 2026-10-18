"""Prior conditional SOT experiment, common archived SOT TOTAL/TEAM universe."""
import argparse,math,sys
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.conditional_sot import ConversionHistory,paired_actual,bank,distribution
from betting_v2.research_probability_recalibration import ROOT,PRIOR,OCTOBER,FOLDS,MonthlyModels
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.cross_market_inputs import valid_quote
from betting_v2.research_context_market import partitions,eligible,conservative_winner
from betting_v2.research_cross_market import prepared_law,law_value
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.research_scope_expansion import select_scope
from betting_v2.research_git_archive import absolute_time,settle_selected
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.research_app_markets import outcome_extended
from betting_v2.audit_probability_trial import canonical_summary

KINDS=('stat','market','conditional')

def verify(out):
    binding=read(out/'input_bindings.json');changed=[p for p,h in binding['files'].items() if digest((ROOT/p).read_bytes())!=h]
    if changed:raise ValueError('Changed conditional SOT inputs '+str(changed))
    return digest((out/'input_bindings.json').read_bytes())

def feature_frame(history):
    state=ConversionHistory(history,direct.metric_values);targets=defaultdict(list)
    for row,actual in state.events:
        cutoff=int(row['_date']//86400)*86400-86400;targets[cutoff].append((row,actual))
    frame=[]
    for cutoff,batch in sorted(targets.items()):
        state.advance(cutoff)
        for r,(shots,sot) in batch:
            frame.append(dict(state.features(r['_home'],r['_away']),fixture=f"{r['_date']}:{r['_home']}:{r['_away']}",kickoff=r['_date'],shots=shots,sot=sot))
    return frame

def scores(rows):
    if not rows:return dict(events=0,fixtures=0)
    counts=Counter(r['fixture'] for r in rows);weights=np.asarray([1/counts[r['fixture']] for r in rows]);weights/=weights.sum()
    target=np.asarray([[r['y'],r['push'],1-r['y']-r['push']] for r in rows]);result=dict(events=len(rows),fixtures=len(counts));losses={}
    for kind in KINDS:
        a=np.asarray([r['probabilities'][kind] for r in rows]);a=np.c_[a,1-a.sum(axis=1)]
        if not np.isfinite(a).all() or np.any(a < -1e-9):raise ValueError('Invalid SOT probability partition')
        ll=-(target*np.log(np.clip(a,1e-12,1))).sum(axis=1);losses[kind]=ll
        result[kind]=dict(logLoss=float(weights@ll),brier=float(weights@((a-target)**2).sum(axis=1)))
    for base in ('stat','market'):
        pairs=defaultdict(list)
        for i,r in enumerate(rows):pairs[r['fixture']].append(losses['conditional'][i]-losses[base][i])
        a=np.asarray([np.mean(pairs[k]) for k in sorted(pairs)]);rng=np.random.default_rng(18102026)
        boot=[float(a[rng.integers(len(a),size=len(a))].mean()) for _ in range(2000)] if len(a)>=30 else []
        result['conditionalMinus'+base.title()]=dict(mean=float(a.mean()),fixture95=list(map(float,np.quantile(boot,[.025,.975]))) if boot else None)
    return result

def unwrap(j):return j.joint if hasattr(j,'joint') else j

def count_loss(j,actual):
    h,a=map(int,actual);pmf=unwrap(j).pmf
    return -math.log(max(float(pmf[h,a]) if h<pmf.shape[0] and a<pmf.shape[1] else 0.,1e-15))

def run(out,signature):
    history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    fitpath=out/'parameter_banks.json'
    if fitpath.exists():
        doc=read(fitpath)
        if doc['signature']!=signature:raise ValueError('Conditional parameter cache mismatch')
        fits=doc['fits']
    else:
        frame=feature_frame(history);write(out/'training_features.json.gz',frame,zipped=True);fits={}
        for day in FOLDS:
            fits[day]=bank(frame,absolute_time(day+'T00:00:00Z'))
            write(fitpath,dict(signature=signature,fits=fits,trainingFixtures=len(frame)))
            print('CONDITIONAL_FIT',day,fits[day]['point'],flush=True)
    # Incomplete monthly cache is not silently accepted after a failed fit.
    for day in FOLDS:
        if day not in fits:
            frame=read(out/'training_features.json.gz');fits[day]=bank(frame,absolute_time(day+'T00:00:00Z'))
            write(fitpath,dict(signature=signature,fits=fits,trainingFixtures=len(frame)))
    profiles={d:{m:read(PRIOR/f'{m}_{d}_coupled_profiles.json')['profiles'] if d!=FOLDS[-1] else read(ROOT/f'reports/betting_v2/combined_candidate_20261016/{m}_profiles.json') for m in ('SHOTS','SOT')} for d in FOLDS}
    results={}
    for packet in ('earliest','latest'):
        quotes=read(PRIOR/f'quotes_{packet}.json.gz')+read(OCTOBER/f'quotes_{packet}.json.gz');quotes=[q for q in quotes if valid_quote(q) is None]
        groups,audit=partitions(quotes);groups=[g for g in groups if g['over']['spec'][0]=='SOT'];clocks=defaultdict(set)
        for g in groups:
            q=g['over'];clocks[q['fixture']].add(tuple(q[k] for k in ('sourceReceipt','providerEventId','observed','kickoff','featureCutoff')))
        rejected={f for f,v in clocks.items() if len(v)!=1};groups=[g for g in groups if g['over']['fixture'] not in rejected]
        byday=defaultdict(lambda:defaultdict(list))
        for g in groups:byday[g['over']['featureCutoff']][g['over']['fixture']].append(g)
        checkpoint=out/f'checkpoint_{packet}.json.gz'
        saved=read(checkpoint) if checkpoint.exists() else dict(signature=signature,done=[],options={},proper=[],countRows=[],diagnostics={})
        if saved['signature']!=signature:raise ValueError('Conditional checkpoint mismatch')
        done=set(saved['done']);options=defaultdict(list,saved['options']);proper=saved['proper'];countrows=saved['countRows'];diagnostics=Counter(saved['diagnostics'])
        models=MonthlyModels(history,profiles);conversion=ConversionHistory(history,direct.metric_values);favmap=fixture_favourite(quotes)
        for cutoff,fixtures in sorted(byday.items()):
            if cutoff in done:continue
            models.advance(cutoff);conversion.advance(cutoff);model=fits[models.fold]
            if not model['point']['fitted']:raise ValueError('Insufficient prior conditional fit')
            for fixture,contracts in fixtures.items():
                q=contracts[0]['over'];h,a=q['_home'],q['_away'];parent=models.prediction(h,a,'SHOTS','coupled');baseline=models.prediction(h,a,'SOT','coupled')
                if parent is None or baseline is None:diagnostics['missingFrozenPrediction']+=1;continue
                feature=conversion.features(h,a);joint=distribution(unwrap(parent['prepared'][0][0]),feature,model['point']);prepared=prepared_law(joint);replicas=None
                actual=direct.metric_values(truth[fixture],'SOT') if fixture in truth else None
                if actual is not None:countrows.append(dict(fixture=fixture,fold=models.fold,stat=count_loss(baseline['prepared'][0][0],actual),conditional=count_loss(joint,actual)))
                for g in contracts:
                    probabilities={};market=g['reference']['q']
                    for positive,offered in [(True,g['over']),(False,g['under'])]:
                        spec=offered['spec'];fav=favmap.get((fixture,offered['bookmaker'],offered['sourceReceipt']))
                        base=forecast(baseline['rows'][0],baseline['profiles'][0],spec,baseline['prepared'][0]);new=law_value(prepared,spec)
                        for kind in KINDS:
                            v=new if kind=='conditional' else base;p=v['winProbability'];push=v['pushProbability']
                            if kind=='market':p=(market if positive else 1-market)*(1-push)
                            if positive:probabilities[kind]=[p,push]
                            candidate=eligible(offered,v,baseline['full'],fav,p,push)
                            if candidate is None:continue
                            if kind=='conditional':
                                if replicas is None:replicas=[prepared_law(distribution(unwrap(pr[0]),feature,m)) for pr,m in zip(parent['prepared'][1:],model['replicas'])]
                                values=[law_value(pr,spec) for pr in replicas];wins=[x['winProbability'] for x in values]
                            else:
                                values=[forecast(r,pf,spec,pr) for r,pf,pr in zip(baseline['rows'][1:],baseline['profiles'][1:],baseline['prepared'][1:])]
                                wins=[x['winProbability'] if kind=='stat' else (market if positive else 1-market)*(1-x['pushProbability']) for x in values]
                            support=None if not fav else min(v['scenarios'][fav]['winProbability'] or 0.,float(np.quantile([x['scenarios'][fav]['winProbability'] or 0. for x in values],.1)))
                            candidate['_lower']=dict(p=min(p,float(np.quantile(wins,.1))),push=min(push,float(np.quantile([x['pushProbability'] for x in values],.1))),support=support)
                            options[kind].append(candidate)
                    if actual is not None:
                        y,ap=outcome_extended(*actual,g['over']['spec']);proper.append(dict(fixture=fixture,fold=models.fold,scope=g['over']['spec'][1],y=y,push=ap,probabilities=probabilities))
            done.add(cutoff)
            write(checkpoint,dict(signature=signature,done=sorted(done),options=dict(options),proper=proper,countRows=countrows,diagnostics=dict(diagnostics)),zipped=True)
            write(out/'progress.json',dict(packet=packet,completedDays=len(done),totalDays=len(byday),providerCalls=0,llmCalls=0))
            print('CONDITIONAL_DAY',packet,cutoff,{k:len(v) for k,v in options.items()},flush=True)
        reports={}
        for kind in KINDS:
            winners=select_scope(options[kind])
            for mode,picks in [('point',winners),('conservative',[p for w in winners if (p:=conservative_winner(w)) is not None])]:
                settled,missing=settle_selected(picks,truth);reports[kind+'_'+mode]=dict(overall=canonical_summary(settled),missingActual=missing,picks=settled,
                    byMonth={d:canonical_summary([q for q in settled if datetime.fromtimestamp(q['kickoff'],timezone.utc).strftime('%Y-%m')==d[:7]]) for d in FOLDS},
                    byScope={s:canonical_summary([q for q in settled if q['spec'][1]==s]) for s in ('TOTAL','TEAM')})
        probability={d:dict(all=scores([r for r in proper if r['fold']==d]),byScope={s:scores([r for r in proper if r['fold']==d and r['scope']==s]) for s in ('TOTAL','TEAM')}) for d in FOLDS}
        counts={d:dict(fixtures=sum(r['fold']==d for r in countrows),**{k:float(np.mean([r[k] for r in countrows if r['fold']==d])) if any(r['fold']==d for r in countrows) else None for k in ('stat','conditional')}) for d in FOLDS}
        write(out/f'priced_{packet}.json',dict(reports=reports,probabilityScores=probability,jointCountScores=counts,partitionAudit=audit,mixedClockFixturesRejected=len(rejected),diagnostics=dict(diagnostics)))
        results[packet]=dict(reports={k:{a:b for a,b in v.items() if a!='picks'} for k,v in reports.items()},probabilityScores=probability,jointCountScores=counts,diagnostics=dict(diagnostics))
    write(out/'summary.json',dict(status='COMPLETE',results=results,primary='conditional_conservative',thresholdsChanged=False,roiOptimized=False,untouchedHoldout=False,deployed=False,providerCalls=0,llmCalls=0,inputSignature=signature))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();run(args.output,verify(args.output))
