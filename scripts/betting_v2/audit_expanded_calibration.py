"""Reconcile frozen banks, all selected outcomes and full proper-score arithmetic."""
import argparse,sys,math
from pathlib import Path
from collections import Counter,defaultdict
import numpy as np
from scipy.special import expit,logit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import ROOT,PRIOR,FOLDS
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_git_archive import absolute_time,settle_selected
from betting_v2.research_context_market import conservative_winner
from betting_v2.research_scope_expansion import select_scope
from betting_v2.expanded_calibration import adjusted


def outcome_key(q):return q['fixture'],tuple(q['spec']),q['bookmaker'],q['sourceReceipt'],q['odd']

def main(out):
    binding=read(out/'input_bindings.json');assert all(digest((ROOT/p).read_bytes())==h for p,h in binding['files'].items())
    summary=read(out/'summary.json');banks=read(out/'parameter_banks.json')['fits'];history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};checks={};fitcalls=successful=0
    for packet in ('earliest','latest'):
        source=read(ROOT/f'reports/betting_v2/market_first_counts_20261018/replay_checkpoint_{packet}.json.gz');doc=read(out/f'priced_{packet}.json.gz')
        for kind,folds in banks[packet].items():
            for fold,metrics in folds.items():
                cutoff=absolute_time(fold+'T00:00:00Z')
                for metric,b in metrics.items():
                    rows=[r for r in source['proper'] if r['metric']==metric and not r['push'] and float(r['fixture'].split(':',1)[0])+86400<=cutoff];n=len({r['fixture'] for r in rows})
                    for f in [b['point'],*b['replicas']]:
                        fitcalls+=1;successful+=int(f['fitted']);assert f['trainingBefore']==cutoff and f['fixtures']==n and f['events']==len(rows) and not f['roiUsed'];assert f['fitted']==(n>=30)
                        if f['fitted']:assert f['maxTrainingKickoff']+86400<=cutoff and .25<=f['theta'][1]<=2 and -2<=f['theta'][0]<=2
        reports={}
        for name,picks in doc['selected'].items():
            kind,variant,mode=name.split('_');options=[]
            for q in source['options'][kind]:
                valid=[d for d in FOLDS[1:] if absolute_time(d+'T00:00:00Z')<=q['featureCutoff']]
                if not valid:continue
                fit=banks[packet][kind][max(valid)][q['spec'][0]]
                if not fit['point']['fitted']:continue
                candidate=q if variant=='raw' else adjusted(q,fit)
                if candidate is not None:options.append(candidate)
            expected=select_scope(options)
            if mode=='conservative':expected=[r for w in expected if (r:=conservative_winner(w)) is not None]
            assert expected==picks;assert len(picks)==len({(r['fixture'],r['spec'][0]) for r in picks})
            for q in picks:assert .6<=q['p'] and 1.8<q['odd']<3 and q['p']*q['odd']+q['push']>1
            graded,missing=settle_selected(picks,truth);report=doc['reports'][name];assert report['picks']==graded and report['missingActual']==missing
            profit=sum(q['y']*q['odd']+q['actualPush']-1 for q in graded);s=report['overall'];assert s['bets']==len(graded) and math.isclose(s.get('profitUnits',0),profit,abs_tol=1e-11)
            assert report['selectedBeforeSettlement']==len(picks);assert math.isclose(report['allSelectedProfitBounds'][0],profit-sum(missing.values()),abs_tol=1e-11)
            reports[name]=dict(bets=len(graded),selected=len(picks),missing=sum(missing.values()),profitUnits=profit)
        # Independent vector math for every recorded three-state score, all outcomes.
        scorechecks=0
        for kind,folds in doc['properScores'].items():
            for fold,groups in folds.items():
                for metric,expected in groups.items():
                    rows=[r for r in source['proper'] if r['fold']==fold and (metric=='overall' or r['metric']==metric) and banks[packet][kind][fold][r['metric']]['point']['fitted']]
                    assert len(rows)==expected['events']
                    if not rows:continue
                    w=np.array([r['probabilities'][kind] for r in rows]);cond=np.clip(w[:,0]/(1-w[:,1]),1e-12,1-1e-12);theta=np.array([banks[packet][kind][fold][r['metric']]['point']['theta'] for r in rows]);new=expit(theta[:,0]+theta[:,1]*logit(cond))*(1-w[:,1]);a=np.c_[w,1-w.sum(axis=1)];b=np.c_[new,w[:,1],1-new-w[:,1]];y=np.array([[r['y'],r['push'],1-r['y']-r['push']] for r in rows]);counts=Counter(r['fixture'] for r in rows);weights=np.array([1/counts[r['fixture']] for r in rows]);weights/=weights.sum()
                    for key,p in [('raw',a),('calibrated',b)]:
                        ll=float(weights@(-(y*np.log(np.clip(p,1e-12,1))).sum(axis=1)));br=float(weights@((p-y)**2).sum(axis=1));assert math.isclose(ll,expected[key]['logLoss'],abs_tol=1e-12) and math.isclose(br,expected[key]['brier'],abs_tol=1e-12)
                    scorechecks+=1
        checks[packet]=dict(reports=reports,properScoreGroupsVerified=scorechecks)
    assert fitcalls==summary['calibratorFitCalls'] and successful==summary['calibratorFits']
    write(out/'verification.json',dict(status='PASS',inputFiles=len(binding['files']),fitCalls=fitcalls,successfulFits=successful,results=checks,independentVectorScoreMath=True,providerCalls=0,llmCalls=0));print('EXPANDED_CALIBRATION_VERIFIED',fitcalls,successful)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
