"""Residual-market follow-up using the already frozen complete event frames."""
import argparse
from collections import defaultdict
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.market_residual import groups,fit_residual,probabilities
from betting_v2.market_calibration import select_ev
from betting_v2.research_market_decisions import attach_reference,score_report
from betting_v2.research_quotes import stats
from betting_v2.validated_walk_forward import FOLDS


def evaluate(rows,fit_for):
    options={a:[] for a in ('residual_ev_60','residual_ev_positive_only_DIAGNOSTIC')};scores=defaultdict(list)
    for partition in groups(rows,for_training=False):
        original=[rows[i] for i in partition['indexes']];fit=fit_for(original[0])
        p=probabilities(partition['stat'],partition['market'],fit['coef'])
        for r,q in zip(original,p):
            win=float(q*(1-r['push']));ev=win*r['odd']+r['push']-1
            if not r['actualPush']:
                scores[r['spec'][0]].append(dict(fixture=r['fixture'],stat=(r['p']/(1-r['push'])-r['y'])**2,
                    market=(r['reference']['q']-r['y'])**2,pool=(q-r['y'])**2))
            if not 1.8<r['odd']<3 or ev<=1e-12:continue
            candidate=dict(r,p=win,ev=ev,confidence=win,pointStatProbability=r['p'],marketProbability=r['reference']['q']*(1-r['push']),
                residualCoef=fit['coef'],tier='HIGH' if win>=.8 else 'CONFIRMED' if win>=.7 else 'DEVELOPING')
            options['residual_ev_positive_only_DIAGNOSTIC'].append(candidate)
            if win>=.6:options['residual_ev_60'].append(candidate)
    results={}
    for arm,candidates in options.items():
        picks=select_ev(candidates)
        results[arm]=dict(overall=stats(picks),byMetric={m:stats([r for r in picks if r['spec'][0]==m]) for m in sorted({r['spec'][0] for r in picks})},picks=picks)
    return dict(arms=results,probabilityScores=score_report(scores))


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--frames',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    prior=read(args.frames/'csv_seed.json.gz');csv=read(args.frames/'csv_frames.json.gz');combined={a:[] for a in ('residual_ev_60','residual_ev_positive_only_DIAGNOSTIC')};fits={};foldreports={}
    for _,day,end in FOLDS:
        cutoff=direct.timestamp(day);rows=[r for r in csv if cutoff<=r['kickoff']<direct.timestamp(end)]
        fit=fit_residual(groups(prior,'GOALS_FT',cutoff),cutoff);fits[day]=fit
        result=evaluate(rows,lambda _:fit);foldreports[day]=result
        for arm,r in result['arms'].items():combined[arm].extend(r['picks'])
        prior.extend(rows)
    csvresult=dict(fits=fits,folds=foldreports,arms={a:dict(overall=stats(p),picks=p) for a,p in combined.items()})
    write(args.output/'csv_results.json',csvresult)
    csvprior=[r for r in prior if r['kickoff']<direct.timestamp('2026-10-01')];receipts={}
    for packet in ('earliest','latest'):
        rows,audit=attach_reference(read(args.frames/('frame_'+packet+'.json.gz'))['rows']);allgroups=groups(csvprior+rows);cache={}
        def fit_for(r):
            metric=r['spec'][0];cutoff=r['featureCutoff'];key=metric,cutoff
            if key not in cache:cache[key]=fit_residual([g for g in allgroups if g['metric']==metric and g['kickoff']<cutoff],cutoff)
            return cache[key]
        result=evaluate(rows,fit_for);result['fits']={m+'::'+str(int(t)):v for (m,t),v in cache.items()};result['referenceAudit']=audit
        receipts[packet]=result;write(args.output/('receipt_'+packet+'_results.json'),result)
    summary=dict(status='COMPLETE',csv={a:r['overall'] for a,r in csvresult['arms'].items()},csvFits=fits,
        receipts={p:dict(results={a:{k:v for k,v in r.items() if k!='picks'} for a,r in doc['arms'].items()},probabilityScores=doc['probabilityScores'],fits=doc['fits']) for p,doc in receipts.items()},
        frameSha256={p.name:digest(p.read_bytes()) for p in args.frames.glob('*frames.json.gz')},
        providerCalls=0,llmCalls=0,newCoreFits=0,roiOptimized=False,untouchedHoldout=False,appChanges=0,
        candidateContext='Added after the convex-pool diagnostic assigned zero FT statistical weights; all periods remain development evidence',
        limitations=['No untouched holdout or executable-price verification','Only complete RESULT/TOTAL/TEAM/BTTS partitions in this experiment',
            'Per-partition residual calibration is not a replacement coherent joint count distribution',
            'No-floor arm is diagnostic only and app thresholds are unchanged','No profitable winner is automatically selected or published'])
    write(args.output/'summary.json',summary)
    print('RESIDUAL_RESULT',summary,flush=True)

if __name__=='__main__':main()
