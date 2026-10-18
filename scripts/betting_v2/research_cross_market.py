"""Chronological cross-market corner model comparison, separate from live trials."""
import argparse,math,sys
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.cross_market_inputs import anchors,valid_quote
from betting_v2.cross_market_counts import feature_vector,bank,distribution,joint_log_probability
from betting_v2.research_probability_recalibration import ROOT,PRIOR,OCTOBER,FOLDS,MonthlyModels
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_context_market import partitions,prematch_context,mean_for,eligible,conservative_winner
from betting_v2.context_market import feature_vector as context_features,predict as context_predict
from betting_v2.research_git_archive import absolute_time,settle_selected
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.research_scope_expansion import select_scope
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.research_app_markets import outcome_extended
from betting_v2.audit_probability_trial import canonical_summary

KINDS=('stat','context','market','cross_fixed','cross_dynamic')
NEW=KINDS[-2:]

def historical_context(models,q):
    h,a=q['_home'],q['_away'];core=models.counts['CORNERS'];sot=models.counts['SOT']
    c=[core.features(h,a,'CORNERS',True),core.features(a,h,'CORNERS',False)]
    s=[sot.features(h,a,'SOT',True),sot.features(a,h,'SOT',False)]
    def total(rows,x,y):return sum(.5*(v[x]+v[y]) for v in rows)
    return dict(featureCutoff=models.clock,corner_recent=total(c,'recent_for','recent_opponent_against'),
        corner_venue=total(c,'venue_for','venue_opponent_against'),sot_recent=total(s,'recent_for','recent_opponent_against'),
        sot_home=len(sot.history[h]['SOT']),sot_away=len(sot.history[a]['SOT']))

def prepared_law(law):return law,{s:law.condition(s) for s in ('HOME','AWAY')}

def law_value(prepared,spec):
    joint,conditions=prepared;win,push=joint.evaluate(spec);h,a=joint.means
    return dict(winProbability=win,pushProbability=push,homeMean=h,awayMean=a,
        scenarios={s:dict(conditionMass=mass,winProbability=c.evaluate(spec)[0] if c is not None else None) for s,(c,mass) in conditions.items()})

def probability_scores(rows):
    if not rows:return dict(events=0,fixtures=0)
    counts=Counter(r['fixture'] for r in rows);w=np.asarray([1/counts[r['fixture']] for r in rows]);w/=w.sum()
    y=np.asarray([r['actualWin'] for r in rows]);push=np.asarray([r['actualPush'] for r in rows]);target=np.c_[y,push,1-y-push]
    nonpush=push==0;conditional_weights=w[nonpush];conditional_weights/=conditional_weights.sum() if conditional_weights.size else 1
    result=dict(events=len(rows),fixtures=len(counts),conditionalEvents=int(nonpush.sum()));losses={}
    for kind in KINDS:
        p=np.asarray([[r['probabilities'][kind]['win'],r['probabilities'][kind]['push']] for r in rows]);p=np.c_[p,1-p.sum(axis=1)]
        if np.any(p < -1e-9) or not np.isfinite(p).all():raise ValueError('Invalid forecast probability partition')
        loss=-(target*np.log(np.clip(p,1e-12,1))).sum(axis=1);losses[kind]=loss
        conditional=np.clip(p[nonpush,0]/np.maximum(1-p[nonpush,1],1e-12),1e-12,1-1e-12)
        binary=-y[nonpush]*np.log(conditional)-(1-y[nonpush])*np.log1p(-conditional)
        result[kind]=dict(threeStateLogLoss=float(w@loss),threeStateBrier=float(w@((p-target)**2).sum(axis=1)),
            conditionalLogLoss=float(conditional_weights@binary) if conditional.size else None)
    for baseline in ('stat','market'):
        differences=defaultdict(list)
        for i,r in enumerate(rows):differences[r['fixture']].append(losses['cross_dynamic'][i]-losses[baseline][i])
        a=np.asarray([np.mean(differences[f]) for f in sorted(differences)]);rng=np.random.default_rng(18102026)
        boot=[float(a[rng.integers(len(a),size=len(a))].mean()) for _ in range(2000)] if len(a)>=30 else []
        result['dynamicMinus'+baseline.title()]=dict(mean=float(a.mean()),fixture95=list(map(float,np.quantile(boot,[.025,.975]))) if boot else None)
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();out=args.output
    binding=read(out/'input_bindings.json');signature=digest((out/'input_bindings.json').read_bytes())
    changed=[s for s,h in binding['files'].items() if digest((ROOT/s).read_bytes())!=h]
    if changed:raise ValueError('Changed locked cross-market inputs '+str(changed))
    history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    profiles={d:{'CORNERS':read(PRIOR/f'CORNERS_{d}_coupled_profiles.json')['profiles'] if d!=FOLDS[-1]
        else read(ROOT/'reports/betting_v2/combined_candidate_20261016/CORNERS_profiles.json')} for d in FOLDS}
    source=ROOT/'reports/betting_v2/context_market_model_20261018';results={}
    for packet in ('earliest','latest'):
        quotes=read(PRIOR/f'quotes_{packet}.json.gz')+read(OCTOBER/f'quotes_{packet}.json.gz')
        quotes=[q for q in quotes if valid_quote(q) is None];contexts,anchor_audit=anchors(quotes)
        byfixture=defaultdict(list)
        for q in quotes:byfixture[q['fixture']].append(q)
        groups,partition_audit=partitions(quotes);groups_byfixture=defaultdict(list)
        for g in groups:
            if g['over']['spec'][0]=='CORNERS':groups_byfixture[g['over']['fixture']].append(g)
        favourites=fixture_favourite(quotes);old=read(source/f'parameter_banks_{packet}.json')['fits']
        checkpoint=out/f'checkpoint_{packet}.json.gz'
        saved=read(checkpoint) if checkpoint.exists() else dict(signature=signature,frame=[],options={},fits={},proper=[],countRows=[],done=[],diagnostics={})
        if saved['signature']!=signature:raise ValueError('Cross-market checkpoint input signature mismatch')
        frame=saved['frame'];options=defaultdict(list,saved['options']);fits=saved['fits'];proper=saved['proper'];count_rows=saved['countRows'];done=set(saved['done']);diagnostics=Counter(saved['diagnostics'])
        models=MonthlyModels(history,profiles);days=sorted({c['featureCutoff'] for c in contexts.values()})
        for cutoff in days:
            if cutoff in done:continue
            models.advance(cutoff);start=absolute_time(models.fold+'T00:00:00Z')
            if models.fold not in fits:
                fits[models.fold]={k:bank(frame,start,dynamic=k=='cross_dynamic') for k in NEW}
                print('CROSS_FIT',packet,models.fold,{k:v['point']['fixtures'] for k,v in fits[models.fold].items()},flush=True)
            params=fits[models.fold];context_model=old.get(models.fold,{}).get('context',{})
            active=all(v['point']['fitted'] for v in params.values()) and context_model.get('point',{}).get('fitted',False)
            for fixture,anchor in contexts.items():
                if anchor['featureCutoff']!=cutoff:continue
                q=byfixture[fixture][0];pred=models.prediction(q['_home'],q['_away'],'CORNERS','coupled')
                if pred is None:diagnostics['missingBaseline']+=1;continue
                hist=historical_context(models,q);record=dict(feature_vector(anchor,pred['rows'][0]['means'],hist),
                    fixture=fixture,kickoff=anchor['kickoff'],featureCutoff=cutoff)
                new_prepared={k:prepared_law(distribution(record,v['point'])) for k,v in params.items()} if active else {}
                replica_cache={}
                for g in groups_byfixture.get(fixture,[]) if active else []:
                    qo=g['over'];spec=qo['spec'];ref=g['reference'];market=ref['q'];row,profile,pr=pred['rows'][0],pred['profiles'][0],pred['prepared'][0]
                    if any(qo[k]!=anchor[k] for k in ('sourceReceipt','observed','kickoff','featureCutoff')):raise ValueError('Cross-market fixture clock mismatch')
                    ov=forecast(row,profile,spec,pr);context=prematch_context(models,qo,g['margin'],ref['independent'])
                    def baseline_probability(value,over_value,kind,model=context_model['point']):
                        if kind=='stat':return value['winProbability'],value['pushProbability']
                        probability=market if kind=='market' else context_predict(context_features(qo,
                            over_value['winProbability']/(1-over_value['pushProbability']),market,mean_for(over_value,spec),context),market,model)
                        return (probability if value is over_value or value.get('_isOver',False) else 1-probability)*(1-value['pushProbability']),value['pushProbability']
                    proper_prob={}
                    for offered in (g['over'],g['under']):
                        offered_spec=offered['spec'];isover=offered_spec[2]=='OVER';base=ov if isover else forecast(row,profile,offered_spec,pr)
                        base=dict(base,_isOver=isover)
                        fav=favourites.get((offered['fixture'],offered['bookmaker'],offered['sourceReceipt']))
                        for kind in KINDS:
                            value=law_value(new_prepared[kind],offered_spec) if kind in NEW else base
                            if kind in NEW:win,push=value['winProbability'],value['pushProbability']
                            else:win,push=baseline_probability(base,ov,kind)
                            if isover:proper_prob[kind]=dict(win=win,push=push)
                            candidate=eligible(offered,value,pred['full'],fav,win,push)
                            if candidate is None:continue
                            if kind in NEW:
                                if kind not in replica_cache:
                                    replica_cache[kind]=[prepared_law(distribution(feature_vector(anchor,r['means'],hist),m))
                                        for r,m in zip(pred['rows'][1:],params[kind]['replicas'])]
                                variants=[law_value(prep,offered_spec) for prep in replica_cache[kind]]
                                wins=[v['winProbability'] for v in variants]
                            else:
                                variants=[forecast(r,p,offered_spec,prepared) for r,p,prepared in zip(pred['rows'][1:],pred['profiles'][1:],pred['prepared'][1:])]
                                over_variants=[forecast(r,p,spec,prepared) for r,p,prepared in zip(pred['rows'][1:],pred['profiles'][1:],pred['prepared'][1:])]
                                wins=[]
                                for i,(v,over_v) in enumerate(zip(variants,over_variants)):
                                    if kind=='stat':wins.append(v['winProbability'])
                                    else:
                                        prob=market if kind=='market' else context_predict(context_features(qo,over_v['winProbability']/(1-over_v['pushProbability']),market,mean_for(over_v,spec),context),market,context_model['replicas'][i])
                                        wins.append((prob if isover else 1-prob)*(1-v['pushProbability']))
                            low_support=None if not fav else min(value['scenarios'][fav]['winProbability'] or 0.,float(np.quantile([v['scenarios'][fav]['winProbability'] or 0. for v in variants],.1)))
                            candidate['_lower']=dict(p=min(win,float(np.quantile(wins,.1))),push=min(push,float(np.quantile([v['pushProbability'] for v in variants],.1))),support=low_support)
                            options[kind].append(candidate)
                    actual=direct.metric_values(truth[fixture],'CORNERS') if fixture in truth else None
                    if actual is not None:
                        y,ap=outcome_extended(*actual,spec)
                        proper.append(dict(fixture=fixture,kickoff=anchor['kickoff'],month=models.fold,scope=spec[1],independent=ref['independent'],actualWin=y,actualPush=ap,probabilities=proper_prob))
                actual=direct.metric_values(truth[fixture],'CORNERS') if fixture in truth else None
                if actual is not None:
                    labelled=dict(record,actual=list(actual))
                    if active:
                        joint=pred['prepared'][0][0];pmf=joint.joint.pmf if hasattr(joint,'joint') else joint.pmf;h,a=map(int,actual)
                        base_log=math.log(max(float(pmf[h,a]) if h<pmf.shape[0] and a<pmf.shape[1] else 0.,1e-15))
                        count_rows.append(dict(fixture=fixture,month=models.fold,stat=-base_log,
                            **{k:-joint_log_probability(labelled,v['point']) for k,v in params.items()}))
                    frame.append(labelled)
                else:diagnostics['missingCornerLabel']+=1
            done.add(cutoff)
            write(checkpoint,dict(signature=signature,frame=frame,options=dict(options),fits=fits,proper=proper,countRows=count_rows,done=sorted(done),diagnostics=dict(diagnostics)),zipped=True)
            write(out/'progress.json',dict(packet=packet,completedDays=len(done),totalDays=len(days),selectedCandidates={k:len(v) for k,v in options.items()},providerCalls=0,llmCalls=0))
            print('CROSS_DAY',packet,cutoff,{k:len(v) for k,v in options.items()},flush=True)
        reports={}
        for kind in KINDS:
            winners=select_scope(options[kind])
            for suffix,picks in [('point',winners),('conservative',[p for w in winners if (p:=conservative_winner(w)) is not None])]:
                scored,missing=settle_selected(picks,truth)
                reports[kind+'_'+suffix]=dict(overall=canonical_summary(scored),selectedBeforeSettlement=len(picks),missingActual=missing,
                    byMonth={d:canonical_summary([r for r in scored if datetime.fromtimestamp(r['kickoff'],timezone.utc).strftime('%Y-%m')==d[:7]]) for d in FOLDS[1:]},
                    byScope={s:canonical_summary([r for r in scored if r['spec'][1]==s]) for s in ('TOTAL','TEAM')},picks=scored)
        probability={d:dict(all=probability_scores([r for r in proper if r['month']==d]),
            independent=probability_scores([r for r in proper if r['month']==d and r['independent']])) for d in FOLDS[1:]}
        count_scores={d:dict(fixtures=sum(r['month']==d for r in count_rows),**{k:float(np.mean([r[k] for r in count_rows if r['month']==d])) for k in ('stat',*NEW)}) for d in FOLDS[1:]}
        write(out/f'priced_{packet}.json',dict(reports=reports,probabilityScores=probability,jointCountScores=count_scores,anchorAudit=anchor_audit,partitionAudit=partition_audit,diagnostics=dict(diagnostics)))
        write(out/f'parameter_banks_{packet}.json',dict(fits=fits,deployed=False))
        results[packet]=dict(reports={k:{a:b for a,b in v.items() if a!='picks'} for k,v in reports.items()},probabilityScores=probability,jointCountScores=count_scores,anchorAudit=anchor_audit,diagnostics=dict(diagnostics))
    write(out/'summary.json',dict(status='COMPLETE',results=results,providerCalls=0,llmCalls=0,thresholdsChanged=False,roiOptimized=False,
        untouchedHoldout=False,deployed=False,primary='cross_dynamic_conservative',inputSignature=signature))

if __name__=='__main__':main()
