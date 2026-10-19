"""Use all recovered prior forecast events for a frozen count decision calibrator."""
from collections import Counter,defaultdict
import math
import numpy as np
from betting_v2.probability_recalibration import bank,probability,decision,SEED
from betting_v2.research_scope_expansion import select_scope
from betting_v2.research_context_market import conservative_winner


def training_frame(proper,kind):
    rows=[]
    for r in proper:
        win,push=r['probabilities'][kind]
        if not (0<=win<=1 and 0<=push<1 and win+push<=1+1e-10):raise ValueError('Invalid source probability')
        if r['push']:continue # Conditional calibration; push probability stays fixed.
        rows.append(dict(metric=r['metric'],scope='ALL',fixture=r['fixture'],kickoff=float(r['fixture'].split(':',1)[0]),pOver=win/(1-push),yOver=r['y']))
    return rows


def fit_bank(frame,metric,cutoff):
    return bank(frame,metric,'ALL',cutoff)


def adjusted(q,fit):
    if not fit['point']['fitted']:return None
    pcal=probability(q['spec'],q['p'],q['push'],fit['point']['theta'])
    result=decision(q,pcal)
    if result is None:return None
    lower=q['_lower'];base=lower['p']
    # A lower sensitivity envelope, not a newly calibrated joint distribution.
    mapped=[probability(q['spec'],base,q['push'],f['theta']) for f in fit['replicas']]
    mapped_point=probability(q['spec'],base,q['push'],fit['point']['theta'])
    result['_lower']=dict(lower,p=min(base,result['p'],mapped_point,float(np.quantile(mapped,.1))))
    result['uncalibratedPointP']=q['p'];result['calibratedPointP']=pcal
    return result


def arms(options):
    winners=select_scope(options)
    return dict(point=winners,conservative=[q for w in winners if (q:=conservative_winner(w)) is not None])


def proper_scores(rows,kind,fits):
    losses=defaultdict(list);raw_brier=defaultdict(list);new_brier=defaultdict(list)
    for r in rows:
        fit=fits.get(r['metric'])
        if not fit or not fit['point']['fitted']:continue
        win,push=r['probabilities'][kind];spec=[r['metric'],'TOTAL','OVER','',0.5]
        new=probability(spec,win,push,fit['point']['theta'])
        a=np.array([win,push,1-win-push]);b=np.array([new,push,1-new-push]);y=np.array([r['y'],r['push'],1-r['y']-r['push']])
        if np.min(b)<-1e-9 or not np.isfinite(b).all():raise ValueError('Invalid calibrated partition')
        lo=float(-(y*np.log(np.clip(a,1e-12,1))).sum());ln=float(-(y*np.log(np.clip(b,1e-12,1))).sum())
        losses[r['fixture']].append([lo,ln]);raw_brier[r['fixture']].append(float(((a-y)**2).sum()));new_brier[r['fixture']].append(float(((b-y)**2).sum()))
    if not losses:return dict(events=0,fixtures=0)
    means=np.array([np.mean(losses[k],axis=0) for k in sorted(losses)]);diff=means[:,1]-means[:,0];rng=np.random.default_rng(SEED)
    samples=[float(np.mean(rng.choice(diff,size=len(diff)))) for _ in range(2000)]
    return dict(events=sum(map(len,losses.values())),fixtures=len(losses),raw=dict(logLoss=float(means[:,0].mean()),brier=float(np.mean([np.mean(v) for v in raw_brier.values()]))),
        calibrated=dict(logLoss=float(means[:,1].mean()),brier=float(np.mean([np.mean(v) for v in new_brier.values()]))),
        calibratedMinusRawLogLoss=float(diff.mean()),fixture95=np.quantile(samples,[.025,.975]).tolist(),
        interval='Paired 2000 seeded fixture resamples; not adjusted for prior model search')
