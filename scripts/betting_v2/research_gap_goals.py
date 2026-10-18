"""Price archived goal/BTTS contracts using prior-only GAP goal offsets."""
import argparse,sys
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_gap_attack import verify
from betting_v2.gap_goal_rates import bank,predict
from betting_v2.cross_market_inputs import valid_quote,no_vig
from betting_v2.research_probability_recalibration import PRIOR,OCTOBER,FOLDS
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write
from betting_v2.market_calibration import references
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.exact_forecast_adapter import prepare,forecast
from betting_v2.research_app_markets import outcome_extended
from betting_v2.research_scope_expansion import select_scope
from betting_v2.research_git_archive import absolute_time,settle_selected
from betting_v2.audit_probability_trial import canonical_summary

KINDS=('stat','market','gap')
POLICIES={'strong60':.6,'ev55_research':.55}

def goal_partitions(quotes):
    refs,audit=references(quotes);groups=defaultdict(list);result=[];clocks=defaultdict(set)
    for i,q in enumerate(quotes):
        m,s,side,team,line=q['spec']
        if m=='GOALS_FT' and s in ('TOTAL','TEAM','BTTS'):
            groups[q['fixture'],q['sourceReceipt'],q['bookmaker'],s,team,line].append((i,q))
            clocks[q['fixture']].add((q['sourceReceipt'],q['providerEventId'],q['observed'],q['kickoff'],q['featureCutoff']))
    for key,items in groups.items():
        if len(clocks[key[0]])!=1:continue
        sides=('YES','NO') if key[3]=='BTTS' else ('OVER','UNDER');pair=no_vig([q for _,q in items],sides)
        if pair is None:continue
        chosen={s:next((i,q) for i,q in items if q['spec'][2]==s) for s in sides};i,pos=chosen[sides[0]];_,neg=chosen[sides[1]]
        if i not in refs:continue
        if any(pos[k]!=neg[k] for k in ('providerEventId','observed','featureCutoff','kickoff')):raise ValueError('Mixed goal partition clock')
        result.append(dict(positive=pos,negative=neg,reference=refs[i]))
    return result,audit

def eligible(q,value,full,fav,p,push,floor):
    if not 1.8<q['odd']<3 or p<floor or p*q['odd']+push<=1:return None
    support=None;preference=1;metric,scope,side,team,_=q['spec']
    if fav:
        other='AWAY' if fav=='HOME' else 'HOME'
        if full.evaluate(('GOALS_FT','RESULT',fav,'',None))[0]<=full.evaluate(('GOALS_FT','RESULT',other,'',None))[0]:return None
        h,a=value['homeMean'],value['awayMean']
        if (h<=a if fav=='HOME' else a<=h):return None
        support=value['scenarios'][fav]['winProbability']
        # Keep the existing 60% favorite-scenario guard in BOTH policies.
        if support is None or support<.6:return None
        if team:preference=0 if (team==fav and side=='OVER') or (team!=fav and side=='UNDER') else 2
        elif scope=='BTTS':preference=0 if side=='NO' else 2
    confidence=min(p,support) if support is not None else p
    return dict(q,p=p,push=push,ev=p*q['odd']+push-1,confidence=confidence,preference=preference)

def conservative(winner,floor):
    lower=winner['_lower'];p=min(winner['p'],lower['p']);push=min(winner['push'],lower['push'])
    if p<floor or p*winner['odd']+push<=1 or (lower['support'] is not None and lower['support']<.6):return None
    return dict(winner,p=p,push=push,ev=p*winner['odd']+push-1,confidence=min(p,lower['support']) if lower['support'] is not None else p)

def proper_scores(rows):
    if not rows:return dict(events=0,fixtures=0)
    counts=Counter(r['fixture'] for r in rows);w=np.asarray([1/counts[r['fixture']] for r in rows]);w/=w.sum()
    target=np.asarray([[r['win'],r['push'],1-r['win']-r['push']] for r in rows]);result=dict(events=len(rows),fixtures=len(counts));losses={}
    for kind in KINDS:
        a=np.asarray([[r['probabilities'][kind][0],r['probabilities'][kind][1]] for r in rows]);a=np.c_[a,1-a.sum(axis=1)]
        if not np.isfinite(a).all() or np.any(a < -1e-9):raise ValueError('Invalid goal probability partition')
        ll=-(target*np.log(np.clip(a,1e-12,1))).sum(axis=1);losses[kind]=ll
        result[kind]=dict(logLoss=float(w@ll),brier=float(w@((a-target)**2).sum(axis=1)))
    for base in ('stat','market'):
        pairs=defaultdict(list)
        for i,r in enumerate(rows):pairs[r['fixture']].append(losses['gap'][i]-losses[base][i])
        a=np.asarray([np.mean(pairs[k]) for k in sorted(pairs)]);rng=np.random.default_rng(18102026)
        boot=[float(a[rng.integers(len(a),size=len(a))].mean()) for _ in range(2000)] if len(a)>=30 else []
        result['gapMinus'+base.title()]=dict(mean=float(a.mean()),fixture95=list(map(float,np.quantile(boot,[.025,.975]))) if boot else None)
    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();out=args.output;signature=verify(out)
    training=read(out/'goal_training_features.json.gz');profiles=read(out/'goal_profiles.json');truth=read(PRIOR/'history.json.gz')
    truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in truth}
    params={d:bank(training,absolute_time(d+'T00:00:00Z')) for d in FOLDS}
    write(out/'goal_rate_banks.json',dict(signature=signature,fits=params));results={}
    for packet in ('earliest','latest'):
        rows=read(out/f'goal_features_{packet}.json.gz');features={r['fixture']:r for r in rows}
        quotes=read(PRIOR/f'quotes_{packet}.json.gz')+read(OCTOBER/f'quotes_{packet}.json.gz');quotes=[q for q in quotes if valid_quote(q) is None]
        groups,audit=goal_partitions(quotes);byfixture=defaultdict(list)
        for g in groups:byfixture[g['positive']['fixture']].append(g)
        favourites=fixture_favourite(quotes);options=defaultdict(list);proper=[];diagnostics=Counter()
        for fixture,groups in byfixture.items():
            r=features.get(fixture)
            if not r:diagnostics['missingLaggedFeature']+=1;continue
            model=params[r['fold']]
            if not model['point']['fitted']:continue
            if any(g['positive']['featureCutoff']!=r['featureCutoff'] for g in groups):raise ValueError('GAP priced feature clock mismatch')
            profile=profiles[r['fold']];template=dict(metric='GOALS_FT',regime=r['regime'])
            stat_rows=[dict(template,means=mu) for mu in r['baseMeanBank']]
            gap_rows=[dict(template,means=predict(r,model['point']))]+[dict(template,means=predict(r,m,i+1)) for i,m in enumerate(model['replicas'])]
            prepared={'stat':[prepare(v,profile) for v in stat_rows],'gap':[prepare(v,profile) for v in gap_rows]}
            for g in groups:
                pos=g['positive'];spec=pos['spec'];probabilities={}
                for is_positive,q in [(True,pos),(False,g['negative'])]:
                    fav=favourites.get((fixture,q['bookmaker'],q['sourceReceipt']))
                    for kind in KINDS:
                        source='stat' if kind=='market' else kind;rr=stat_rows if source=='stat' else gap_rows;prepared_source=prepared[source]
                        values=[forecast(row,profile,q['spec'],p) for row,p in zip(rr,prepared_source)];v=values[0]
                        if kind=='market':
                            probability=g['reference']['q'] if is_positive else 1-g['reference']['q'];wins=[probability*(1-x['pushProbability']) for x in values]
                        else:wins=[x['winProbability'] for x in values]
                        p,push=wins[0],v['pushProbability']
                        if is_positive:probabilities[kind]=[p,push]
                        support=None if not fav else min(v['scenarios'][fav]['winProbability'] or 0.,float(np.quantile([x['scenarios'][fav]['winProbability'] or 0. for x in values[1:]],.1)))
                        lower=dict(p=min(p,float(np.quantile(wins[1:],.1))),push=min(push,float(np.quantile([x['pushProbability'] for x in values[1:]],.1))),support=support)
                        for policy,floor in POLICIES.items():
                            candidate=eligible(q,v,prepared_source[0][0],fav,p,push,floor)
                            if candidate is not None:options[kind+'_'+policy].append(dict(candidate,_lower=lower))
                if r['actual'] is not None:
                    y,ap=outcome_extended(*r['actual'],spec);proper.append(dict(fixture=fixture,fold=r['fold'],scope=spec[1],win=y,push=ap,probabilities=probabilities))
        reports={}
        for kind in KINDS:
            for policy,floor in POLICIES.items():
                winners=select_scope(options[kind+'_'+policy])
                for mode,picks in [('point',winners),('conservative',[p for w in winners if (p:=conservative(w,floor)) is not None])]:
                    settled,missing=settle_selected(picks,truth);reports[kind+'_'+policy+'_'+mode]=dict(overall=canonical_summary(settled),missingActual=missing,picks=settled,
                        byMonth={d:canonical_summary([q for q in settled if datetime.fromtimestamp(q['kickoff'],timezone.utc).strftime('%Y-%m')==d[:7]]) for d in FOLDS[1:]},
                        byScope={s:canonical_summary([q for q in settled if q['spec'][1]==s]) for s in ('TOTAL','TEAM','BTTS')})
        scores={d:dict(all=proper_scores([r for r in proper if r['fold']==d]),byScope={s:proper_scores([r for r in proper if r['fold']==d and r['scope']==s]) for s in ('TOTAL','TEAM','BTTS')}) for d in FOLDS[1:]}
        write(out/f'priced_{packet}.json',dict(reports=reports,probabilityScores=scores,referenceAudit=audit,diagnostics=dict(diagnostics)))
        results[packet]=dict(reports={k:{a:b for a,b in v.items() if a!='picks'} for k,v in reports.items()},probabilityScores=scores,diagnostics=dict(diagnostics))
        print('GAP_GOAL_RESULTS',packet,{k:v['overall'] for k,v in reports.items()},flush=True)
    write(out/'summary.json',dict(status='COMPLETE',results=results,primary='gap_strong60_conservative',secondary='ev55_research',
        primaryThresholdsChanged=False,secondaryIsStrong=False,roiOptimized=False,untouchedHoldout=False,deployed=False,providerCalls=0,llmCalls=0,inputSignature=signature))

if __name__=='__main__':main()
