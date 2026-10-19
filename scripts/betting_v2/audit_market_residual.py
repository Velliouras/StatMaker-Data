"""Verify residual data, prior fits, scores, fixed decisions and a bias-only ablation."""
import argparse,sys,math
from pathlib import Path
from collections import Counter
import numpy as np
from scipy.special import expit,logit
from scipy.optimize import brentq
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import ROOT,PRIOR,FOLDS
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_git_archive import absolute_time,settle_selected,COUNT_METRICS
from betting_v2.research_market_residual import offer_key,score
from betting_v2.market_residual import candidate
from betting_v2.market_calibration import references
from betting_v2.expanded_calibration import arms


def bias_only(rows,cutoff):
    counts=Counter(r['fixture'] for r in rows);meta=dict(fixtures=len(counts),events=len(rows),trainingBefore=cutoff)
    if len(counts)<30:return dict(point=dict(meta,fitted=False,theta=[0.,0.]))
    weights=np.array([1/counts[r['fixture']] for r in rows]);weights/=weights.sum();offset=logit([r['marketOver'] for r in rows]);y=np.array([r['yOver'] for r in rows])
    def grad(b):return float(weights@(expit(offset+b)-y)+.02*b)
    b=-2. if grad(-2.)>=0 else 2. if grad(2.)<=0 else brentq(grad,-2.,2.)
    return dict(point=dict(meta,fitted=True,theta=[float(b),0.]))


def vector_scores(rows,parameters):
    if not rows:return None
    counts=Counter(r['fixture'] for r in rows);weights=np.array([1/counts[r['fixture']] for r in rows]);weights/=weights.sum();raw=np.array([r['statWinOver'] for r in rows]);push=np.array([r['modelPush'] for r in rows]);pm=np.array([r['marketOver'] for r in rows]);ps=np.clip([r['statOver'] for r in rows],1e-12,1-1e-12);theta=np.array([parameters[r['metric']]['point']['theta'] for r in rows]);new=expit(logit(pm)+theta[:,0]+theta[:,1]*(logit(ps)-logit(pm)))*(1-push);y=np.array([[r['yOver'],r['actualPush'],1-r['yOver']-r['actualPush']] for r in rows]);result={}
    for name,p in [('stat',np.c_[raw,push,1-raw-push]),('market',np.c_[pm*(1-push),push,(1-pm)*(1-push)]),('residual',np.c_[new,push,1-new-push])]:
        result[name]=dict(logLoss=float(weights@(-(y*np.log(np.clip(p,1e-12,1))).sum(axis=1))),brier=float(weights@((p-y)**2).sum(axis=1)))
    return result


def main(out):
    binding=read(out/'input_bindings.json');assert all(digest((ROOT/p).read_bytes())==h for p,h in binding['files'].items());summary=read(out/'summary.json');allfits=read(out/'parameter_banks.json')['fits'];history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};checks={};bias_reports={};fitcalls=successful=0
    for packet in ('earliest','latest'):
        frame=read(out/f'forecast_frame_{packet}.json.gz');doc=read(out/f'priced_{packet}.json.gz');source=ROOT/'reports/betting_v2/market_first_counts_20261018';quotes=read(source/f'quotes_{packet}.json.gz');saved=read(source/f'replay_checkpoint_{packet}.json.gz');refs,_=references(quotes);lookup={offer_key(q):refs[i] for i,q in enumerate(quotes) if i in refs};biasfits={};reportchecks={}
        for fold,metrics in allfits[packet].items():
            cutoff=absolute_time(fold+'T00:00:00Z');biasfits[fold]={}
            for metric,b in metrics.items():
                rows=[r for r in frame if r['metric']==metric and not r['actualPush'] and r['kickoff']+86400<=cutoff];n=len({r['fixture'] for r in rows});biasfits[fold][metric]=bias_only(rows,cutoff)
                for f in [b['point'],*b['replicas']]:
                    fitcalls+=1;successful+=int(f['fitted']);assert f['events']==len(rows) and f['fixtures']==n and f['trainingBefore']==cutoff and not f['roiUsed'];assert f['fitted']==(n>=30)
                    if f['fitted']:assert f['maxTrainingKickoff']+86400<=cutoff and -2<=f['theta'][0]<=2 and 0<=f['theta'][1]<=1
                if b['point']['fitted']:
                    counts=Counter(r['fixture'] for r in rows);weights=np.array([1/counts[r['fixture']] for r in rows]);weights/=weights.sum();pm=np.array([r['marketOver'] for r in rows]);ps=np.clip([r['statOver'] for r in rows],1e-12,1-1e-12);y=np.array([r['yOver'] for r in rows]);theta=np.array(b['point']['theta']);offset=logit(pm);gap=logit(ps)-offset;z=offset+theta[0]+theta[1]*gap;e=expit(z)-y;loss=float(weights@(np.logaddexp(0,z)-y*z)+.01*(theta@theta));gradient=np.array([weights@e,weights@(e*gap)])+.02*theta;assert math.isclose(loss,b['point']['loss'],abs_tol=1e-10)
                    for x,g,lo,hi in zip(theta,gradient,[-2.,0.],[2.,1.]):assert g>=-2e-6 if x<=lo+1e-8 else g<=2e-6 if x>=hi-1e-8 else abs(g)<2e-6
        for name,picks in doc['selected'].items():
            kind,mode=name.split('_');options=[]
            for q in saved['options']['coupled']:
                valid=[d for d in FOLDS[1:] if absolute_time(d+'T00:00:00Z')<=q['featureCutoff']];ref=lookup.get(offer_key(q))
                if not valid or ref is None:continue
                parameters=allfits[packet][max(valid)][q['spec'][0]]
                if not parameters['point']['fitted']:continue
                c=q if kind=='stat' else candidate(q,ref,parameters,kind)
                if c is not None:options.append(c)
            assert arms(options)[mode]==picks;assert len(picks)==len({(r['fixture'],r['spec'][0]) for r in picks});graded,missing=settle_selected(picks,truth);r=doc['reports'][name];assert r['picks']==graded and r['missingActual']==missing
            profit=sum(q['y']*q['odd']+q['actualPush']-1 for q in graded);assert r['overall']['bets']==len(graded) and math.isclose(r['overall'].get('profitUnits',0),profit,abs_tol=1e-11)
            for q in picks:assert q['p']>=.6 and 1.8<q['odd']<3 and q['p']*q['odd']+q['push']>1
            assert math.isclose(r['allSelectedProfitBounds'][0],profit-sum(missing.values()),abs_tol=1e-11);reportchecks[name]=dict(bets=len(graded),profitUnits=profit,unknown=sum(missing.values()))
        scoreschecked=0;diagnostic={}
        for fold,groups in doc['properScores'].items():
            for metric,expected in groups.items():
                rows=[r for r in frame if r['fold']==fold and (metric=='overall' or r['metric']==metric) and allfits[packet][fold][r['metric']]['point']['fitted']];assert len(rows)==expected['events'];actual=vector_scores(rows,allfits[packet][fold])
                if actual:
                    for kind in ('stat','market','residual'):
                        for key in ('logLoss','brier'):assert math.isclose(actual[kind][key],expected[kind][key],abs_tol=1e-12)
                    scoreschecked+=1
            rows=[r for r in frame if r['fold']==fold];bias_score=score(rows,biasfits[fold]);res_score=doc['properScores'][fold]['overall'];diagnostic[fold]=dict(biasOnly=bias_score,residual=res_score['residual'],residualMinusBiasLogLoss=res_score['residual']['logLoss']-bias_score['residual']['logLoss'])
        checks[packet]=dict(reports=reportchecks,properScoreGroupsVerified=scoreschecked,priorParameterObjectivesAndKKT=True);bias_reports[packet]=dict(banks=biasfits,comparison=diagnostic)
    assert fitcalls==summary['parameterFitCalls'] and successful==summary['successfulFits'];write(out/'verification.json',dict(status='PASS',inputFiles=len(binding['files']),fitCalls=fitcalls,successfulFits=successful,results=checks,independentVectorScores=True,providerCalls=0,llmCalls=0));write(out/'bias_only_diagnostic.json',dict(postHocDiagnostic=True,noStrategySelection=True,results=bias_reports,providerCalls=0,llmCalls=0));print('MARKET_RESIDUAL_VERIFIED',fitcalls,successful)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
