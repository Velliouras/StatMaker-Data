"""Prior-only contextual market model, common-universe benchmarks and selection."""
import argparse
from collections import Counter,defaultdict
from pathlib import Path
from datetime import datetime,timezone
import math,sys,json
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_probability_recalibration import MonthlyModels,FOLDS,PRIOR,OCTOBER,ROOT
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_git_archive import absolute_time,settle_selected
from betting_v2.research_scope_expansion import select_scope
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.market_calibration import references
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.research_app_markets import outcome_extended
from betting_v2.audit_probability_trial import canonical_summary
from betting_v2.context_market import METRICS,FEATURES,feature_vector,bank,predict

KINDS=('stat','market','simple','context')


def partitions(quotes):
    refs,audit=references(quotes);groups=defaultdict(list)
    for i,q in enumerate(quotes):
        m,scope,side,team,line=q['spec']
        if m in METRICS and scope in ('TOTAL','TEAM') and side in ('OVER','UNDER'):
            groups[q['fixture'],q['sourceReceipt'],str(q['providerEventId']),q['bookmaker'],m,scope,team,line].append((i,q))
    result=[];counts=Counter()
    for key,items in groups.items():
        by=defaultdict(list)
        for i,q in items:by[q['spec'][2]].append((i,q))
        if set(by)!=set(('OVER','UNDER')) or any(len({q['odd'] for _,q in values})!=1 for values in by.values()):
            counts['incompleteOrAmbiguousOwnBook']+=1;continue
        io,over=by['OVER'][0];_,under=by['UNDER'][0]
        ref=refs.get(io)
        margin=1/over['odd']+1/under['odd']-1
        if not ref or not 0<ref['q']<1 or not -.05<=margin<=.25:
            counts['missingReferenceOrInvalidMargin']+=1;continue
        if over['observed']!=under['observed'] or over['featureCutoff']!=under['featureCutoff']:
            raise ValueError('Mixed quote partition clocks')
        result.append(dict(over=over,under=under,reference=ref,margin=margin))
    return result,dict(referenceAudit=audit,partitionAudit=dict(counts))


def prematch_context(models,q,margin,independent):
    h,a=q['_home'],q['_away'];metric,scope,_,team,_=q['spec'];core=models.counts[metric]
    hc=core.features(h,a,metric,True);ac=core.features(a,h,metric,False)
    hg=models.ft.features(h,a,'GOALS_FT',True);ag=models.ft.features(a,h,'GOALS_FT',False)
    def average(features,first,second):return .5*(features[first]+features[second])
    if scope=='TEAM':
        c,g=(hc,hg) if team=='HOME' else (ac,ag)
        recent=average(c,'recent_for','recent_opponent_against');venue=average(c,'venue_for','venue_opponent_against')
        xg=average(g,'xg_for','opponent_xg_against');form=average(g,'recent_for','recent_opponent_against');elo=g['elo_gap']
    else:
        recent=sum(average(c,'recent_for','recent_opponent_against') for c in (hc,ac))
        venue=sum(average(c,'venue_for','venue_opponent_against') for c in (hc,ac))
        xg=.5*sum(average(g,'xg_for','opponent_xg_against') for g in (hg,ag))
        form=.5*sum(average(g,'recent_for','recent_opponent_against') for g in (hg,ag));elo=abs(hg['elo_gap'])
    updated=absolute_time(q['marketUpdatedAtUTC'])
    if not updated<=q['observed']<q['kickoff'] or q['featureCutoff']>q['observed']-86400 or models.clock!=q['featureCutoff']:
        raise ValueError('Prematch context or quote timing is invalid')
    return dict(sample_home=len(core.history[h][metric]),sample_away=len(core.history[a][metric]),
        xg_home=len(models.ft.xg[h]),xg_away=len(models.ft.xg[a]),count_recent=recent,count_venue=venue,
        xg_level=xg,goal_form=form,elo=elo,market_updated=updated,margin=margin,independent=independent)


def mean_for(value,spec):
    return value['homeMean'] if spec[3]=='HOME' else value['awayMean'] if spec[3]=='AWAY' else value['homeMean']+value['awayMean']


def eligible(q,value,full,fav,p,push):
    ev=p*q['odd']+push-1
    if not 1.8<q['odd']<3 or p<.6 or ev<=0:return None
    metric,scope,side,team,_=q['spec'];support=None;preference=1
    if fav:
        other='AWAY' if fav=='HOME' else 'HOME'
        if full.evaluate(('GOALS_FT','RESULT',fav,'',None))[0]<=full.evaluate(('GOALS_FT','RESULT',other,'',None))[0]:return None
        attack=metric in ('CORNERS','SHOTS','SOT')
        if attack:
            h,a=value['homeMean'],value['awayMean']
            if (h<=a if fav=='HOME' else a<=h):return None
        support=value['scenarios'][fav]['winProbability']
        if attack and (support is None or support<.6):return None
        if attack and team:preference=0 if (team==fav and side=='OVER') or (team!=fav and side=='UNDER') else 2
    confidence=min(p,support) if support is not None else p
    return dict(q,p=p,push=push,ev=ev,confidence=confidence,preference=preference,
        tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING')


def conservative_winner(winner):
    lower=winner['_lower'];p=min(winner['p'],lower['p']);push=min(winner['push'],lower['push']);ev=p*winner['odd']+push-1
    if p<.6 or ev<=0:return None
    if winner['spec'][0] in ('CORNERS','SHOTS','SOT') and lower['support'] is not None and lower['support']<.6:return None
    confidence=min(p,lower['support']) if lower['support'] is not None else p
    return dict(winner,p=p,push=push,ev=ev,confidence=confidence,
        tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING')


def scores(rows):
    if not rows:return dict(events=0,fixtures=0)
    counts=Counter(r['fixture'] for r in rows);weights=np.asarray([1/counts[r['fixture']] for r in rows]);weights/=weights.sum()
    y=np.asarray([r['yOver'] for r in rows]);result=dict(events=len(rows),fixtures=len(counts))
    losses={}
    for kind in KINDS:
        p=np.clip([r['probabilities'][kind] for r in rows],1e-12,1-1e-12)
        loss=-y*np.log(p)-(1-y)*np.log1p(-p);losses[kind]=loss
        result[kind]=dict(logLoss=float(weights@loss),brier=float(weights@((p-y)**2)))
    paired=defaultdict(list)
    for i,r in enumerate(rows):paired[r['fixture']].append(float(losses['context'][i]-losses['market'][i]))
    values=np.asarray([np.mean(paired[f]) for f in sorted(paired)])
    rng=np.random.default_rng(18102026)
    boot=[float(values[rng.integers(len(values),size=len(values))].mean()) for _ in range(2000)] if len(values)>=30 else []
    result['contextMinusMarketLogLoss']=dict(mean=float(values.mean()),fixture95=list(map(float,np.quantile(boot,[.025,.975]))) if boot else None)
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();out=args.output
    binding=read(out/'input_bindings.json');signature=digest((out/'input_bindings.json').read_bytes())
    changed=[p for p,h in binding['files'].items() if digest((ROOT/p).read_bytes())!=h]
    if changed:raise ValueError('Changed locked context inputs '+str(changed))
    history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    profiles={d:{m:read(PRIOR/f'{m}_{d}_coupled_profiles.json')['profiles'] if d!='2026-10-01'
        else read(ROOT/f'reports/betting_v2/combined_candidate_20261016/{m}_profiles.json') for m in METRICS} for d in FOLDS}
    results={}
    for packet in ('earliest','latest'):
        allquotes=read(PRIOR/f'quotes_{packet}.json.gz')+read(OCTOBER/f'quotes_{packet}.json.gz')
        groups,audit=partitions(allquotes);favourites=fixture_favourite(allquotes)
        checkpoint=out/f'checkpoint_{packet}.json.gz'
        saved=read(checkpoint) if checkpoint.exists() else dict(signature=signature,frame=[],options={},fits={},proper=[],done=[],diagnostics={})
        if saved['signature']!=signature:raise ValueError('Context checkpoint belongs to different inputs')
        frame=saved['frame'];options=defaultdict(list,saved['options']);fits=saved['fits'];proper=saved['proper'];done=set(saved['done']);diagnostics=Counter(saved['diagnostics'])
        models=MonthlyModels(history,profiles)
        for cutoff in sorted({g['over']['featureCutoff'] for g in groups}):
            if cutoff in done:continue
            models.advance(cutoff);start=absolute_time(models.fold+'T00:00:00Z')
            if models.fold not in fits:
                fits[models.fold]={kind:bank(frame,start,kind) for kind in ('simple','context')}
                print('CONTEXT_FIT',packet,models.fold,{k:v['point']['fixtures'] for k,v in fits[models.fold].items()},flush=True)
            params=fits[models.fold];active=all(v['point']['fitted'] for v in params.values())
            for g in groups:
                qo=g['over']
                if qo['featureCutoff']!=cutoff:continue
                spec=qo['spec'];m=spec[0];reference=g['reference'];market=reference['q']
                pred=models.prediction(qo['_home'],qo['_away'],m,'coupled')
                if pred is None:diagnostics['missingPrediction::'+m]+=1;continue
                row,profile,prepared=pred['rows'][0],pred['profiles'][0],pred['prepared'][0]
                over=forecast(row,profile,spec,prepared);push=over['pushProbability']
                if not push<1:diagnostics['degeneratePush']+=1;continue
                context=prematch_context(models,qo,g['margin'],reference['independent'])
                stat=over['winProbability']/(1-push)
                x=feature_vector(qo,stat,market,mean_for(over,spec),context)
                probs=dict(stat=stat,market=market)
                if active:
                    probs.update({k:predict(x,market,v['point']) for k,v in params.items()})
                    for q in (g['over'],g['under']):
                        fav=favourites.get((q['fixture'],q['bookmaker'],q['sourceReceipt']))
                        value=over if q['spec'][2]=='OVER' else forecast(row,profile,q['spec'],prepared)
                        candidates={}
                        for kind,prob in probs.items():
                            win=(prob if q['spec'][2]=='OVER' else 1-prob)*(1-push)
                            candidate=eligible(q,value,pred['full'],fav,win,push)
                            if candidate is not None:candidates[kind]=candidate
                        if candidates:
                            replicas=[forecast(r,p,q['spec'],pr) for r,p,pr in zip(pred['rows'][1:],pred['profiles'][1:],pred['prepared'][1:])]
                            over_replicas=[forecast(r,p,spec,pr) for r,p,pr in zip(pred['rows'][1:],pred['profiles'][1:],pred['prepared'][1:])]
                            lowpush=min(push,float(np.quantile([v['pushProbability'] for v in replicas],.1)))
                            support=None
                            if fav:support=min(value['scenarios'][fav]['winProbability'] or 0.,float(np.quantile([v['scenarios'][fav]['winProbability'] or 0. for v in replicas],.1)))
                            for kind,candidate in candidates.items():
                                wins=[]
                                for i,(v,ov) in enumerate(zip(replicas,over_replicas)):
                                    if kind=='stat':win=v['winProbability']
                                    else:
                                        if kind=='market':prob=market
                                        else:
                                            xb=feature_vector(qo,ov['winProbability']/(1-ov['pushProbability']),market,mean_for(ov,spec),context)
                                            prob=predict(xb,market,params[kind]['replicas'][i])
                                        win=(prob if q['spec'][2]=='OVER' else 1-prob)*(1-v['pushProbability'])
                                    wins.append(win)
                                candidate['_lower']=dict(p=min(candidate['p'],float(np.quantile(wins,.1))),push=lowpush,support=support)
                                options[kind].append(candidate)
                # These labels are read after this partition's current offers are evaluated.
                actual=direct.metric_values(truth[qo['fixture']],m) if qo['fixture'] in truth else None
                if actual is not None:
                    y,actualpush=outcome_extended(*actual,spec)
                    if not actualpush:
                        frame.append(dict(fixture=qo['fixture'],kickoff=qo['kickoff'],featureCutoff=cutoff,x=x,
                            marketOver=market,yOver=y,metric=m,scope=spec[1],independent=reference['independent']))
                        if active:proper.append(dict(fixture=qo['fixture'],kickoff=qo['kickoff'],month=models.fold,
                            metric=m,independent=reference['independent'],yOver=y,probabilities=probs))
            done.add(cutoff)
            write(checkpoint,dict(signature=signature,frame=frame,options=dict(options),fits=fits,proper=proper,done=sorted(done),diagnostics=dict(diagnostics)),zipped=True)
            write(out/'progress.json',dict(packet=packet,completedDays=len(done),totalDays=len({g['over']['featureCutoff'] for g in groups}),providerCalls=0,llmCalls=0,status='RUNNING'))
            print('CONTEXT_DAY',packet,cutoff,{k:len(v) for k,v in options.items()},flush=True)
        reports={}
        for kind in KINDS:
            winners=select_scope(options[kind])
            for suffix,selected in [('point',winners),('conservative',[r for w in winners if (r:=conservative_winner(w)) is not None])]:
                scored,missing=settle_selected(selected,truth)
                reports[kind+'_'+suffix]=dict(overall=canonical_summary(scored),selectedBeforeSettlement=len(selected),missingActual=missing,
                    byMonth={d:canonical_summary([r for r in scored if datetime.fromtimestamp(r['kickoff'],timezone.utc).strftime('%Y-%m')==d[:7]]) for d in FOLDS[1:]},
                    byMetric={m:canonical_summary([r for r in scored if r['spec'][0]==m]) for m in METRICS},picks=scored)
        probability_scores={d:dict(all=scores([r for r in proper if r['month']==d]),independent=scores([r for r in proper if r['month']==d and r['independent']]),
            byMetric={m:scores([r for r in proper if r['month']==d and r['metric']==m]) for m in METRICS}) for d in FOLDS[1:]}
        write(out/f'priced_{packet}.json',dict(reports=reports,probabilityScores=probability_scores,partitionAudit=audit,diagnostics=dict(diagnostics)))
        write(out/f'parameter_banks_{packet}.json',dict(fits=fits,features=list(FEATURES),deployed=False))
        results[packet]=dict(reports={k:{a:b for a,b in v.items() if a!='picks'} for k,v in reports.items()},probabilityScores=probability_scores,partitionAudit=audit)
        print('CONTEXT_RESULT',packet,{k:v['overall'] for k,v in reports.items()},flush=True)
    write(out/'summary.json',dict(status='COMPLETE',results=results,providerCalls=0,llmCalls=0,newRateFits=0,
        thresholdsChanged=False,roiOptimized=False,untouchedHoldout=False,deployed=False,inputSignature=signature))


if __name__=='__main__':main()
