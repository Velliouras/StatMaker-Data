"""Test coupled total/allocation count laws using existing frozen mean models."""
import argparse
from collections import defaultdict
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write
from betting_v2.reliable_counts import METRICS
from betting_v2.paired_count import fit_profile
from betting_v2.research_all_markets import score,aggregate
from betting_v2.validated_walk_forward import distribution,FOLDS
from betting_v2.live_revised import load_revised
from betting_v2.research_app_markets import prepare_extended
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.research_quotes import assess,stats
from betting_v2.research_scope_expansion import select_scope
from betting_v2.market_calibration import select_ev

ROOT=Path(__file__).resolve().parents[2]


def reprice(frame,history,profile):
    result=[]
    for cutoff in sorted({r['featureCutoff'] for r in frame}):
        model,old,_=load_revised(ROOT,history,cutoff);effective=dict(old,**profile);cache={}
        for r in frame:
            if r['featureCutoff']!=cutoff:continue
            metric=r['spec'][0]
            if metric.startswith('GOALS'):result.append(r);continue
            h,a=r['_home'],r['_away'];key=h,a,metric
            if key not in cache:
                pred=model.predict(h,a,metric);full=model.predict(h,a,'GOALS_FT')
                if pred is None or full is None:raise ValueError('Frozen control forecast disappeared')
                regime='XG' if min(len(model.xg[h]),len(model.xg[a]))>=8 else 'ELO_NO_XG'
                target=dict(metric=metric,regime=regime,means=list(pred[:2]));prepared=prepare_extended(target,effective)
                ft=distribution(dict(metric='GOALS_FT',regime=regime,means=list(full[:2])),old)
                cache[key]=target,prepared,ft
            target,prepared,ft=cache[key]
            q=forecast(target,effective,r['spec'],prepared);p,push=q['winProbability'],q['pushProbability']
            selected=assess(r,(target,prepared,ft),effective,r['fav'],'current_preference')
            result.append(dict(r,p=p,push=push,ev=p*r['odd']+push-1,currentEligible=selected is not None,
                confidence=selected['confidence'] if selected else p,tier=selected['tier'] if selected else 'UNSELECTED'))
    return result


def selections(rows):
    eligible=[r for r in rows if r['currentEligible']]
    ev=[r for r in rows if 1.8<r['odd']<3 and r['p']>=.6 and r['ev']>0]
    return {name:dict(overall=stats(picks),picks=picks,byMetric={m:stats([r for r in picks if r['spec'][0]==m]) for m in sorted({r['spec'][0] for r in rows})})
        for name,picks in [('favourite',select_scope(eligible)),('ev_same_gates',select_ev(eligible)),('ev_no_direction_gate',select_ev(ev))]}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--frames',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    source=ROOT/'reports/betting_v2/all_market_revision_20261016';summaries=defaultdict(list);october={}
    for _,day,_ in FOLDS:
        profiles={}
        for metric in METRICS:
            stem=metric+'_'+day;cal=read(source/(stem+'_calibration.json.gz'));rows=read(source/(stem+'_predictions.json.gz'))
            profile=fit_profile(cal);profiles.update(profile)
            old=read(source/(stem+'_profiles.json'))[0]
            before=score(rows,old);after=score(rows,profile)
            item=dict(metric=metric,day=day,fixtures=len(rows),independent=aggregate(list(before.values())),paired=aggregate(list(after.values())),profile=profile)
            summaries[metric].append(item);write(args.output/(stem+'_scores.json'),item)
            print('PAIRED_COUNT_SCORE',metric,day,item['independent']['brier'],item['paired']['brier'],flush=True)
        write(args.output/('profiles_'+day+'.json'),profiles)
        if day=='2026-10-01':october=profiles
    history=direct.load_actual_results(direct.timestamp('2026-10-16'))['GLOBAL'];priced={}
    for packet in ('earliest','latest'):
        baseline=read(args.frames/('frame_'+packet+'.json.gz'))['rows'];updated=reprice(baseline,history,october)
        reports=selections(updated);priced[packet]={a:{k:v for k,v in r.items() if k!='picks'} for a,r in reports.items()}
        write(args.output/('priced_'+packet+'.json'),reports)
    summary=dict(status='COMPLETE',metrics=dict(summaries),priced=priced,providerCalls=0,llmCalls=0,newMeanModelFits=0,
        roiOptimized=False,untouchedHoldout=False,appChanges=0,
        hypothesis='Separate total tempo and conditional home/away allocation; positive or negative count dependence',
        limitations=['Previously inspected development data','Rate models unchanged; only prior calibrated joint law changed',
            'Point candidate only; uncertainty propagation and new live publication are not part of this experiment',
            'All negative results retained; no profitable family/policy chosen after testing'])
    write(args.output/'summary.json',summary);print('PAIRED_PRICED',priced,flush=True)

if __name__=='__main__':main()
