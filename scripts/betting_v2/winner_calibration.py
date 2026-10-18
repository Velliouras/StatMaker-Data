"""Prior-only calibration conditional on one frozen raw winner per family.

The single fitted parameter is a log-odds offset. Pool count metrics without
ROI filters, retain raw push mass, and abstain without trying another contract.
"""
from collections import Counter
import math
import numpy as np
from scipy.special import expit, logit
from scipy.optimize import brentq

MIN_FIXTURES=30
PENALTY=.01
SEED=18102026
LABEL_LAG=86400


def adjusted(win,push,bias):
    if not all(math.isfinite(v) for v in (win,push,bias)) or not 0<=push<1 or not 0<=win<=1-push+1e-10:
        raise ValueError('Invalid winner calibration probability')
    p=np.clip(win/(1-push),1e-12,1-1e-12)
    return float(expit(logit(p)+bias)*(1-push))


def fit(records,cutoff,week_weights=None):
    if any(r['kickoff']+LABEL_LAG>cutoff for r in records):
        raise ValueError('Winner labels cross the fixed lag boundary')
    counts=Counter(r['fixture'] for r in records)
    meta=dict(fixtures=len(counts),events=len(records),trainingBefore=cutoff,labelLagSeconds=LABEL_LAG,
        objective='Fixture-weighted conditional win log loss on frozen raw winners',
        penalty=PENALTY,roiUsed=False,parameter='single log-odds offset',bounds=[-2.,2.])
    if len(counts)<MIN_FIXTURES:return dict(meta,fitted=False,bias=0.,reason='INSUFFICIENT_PRIOR_WINNERS')
    p=np.asarray([r['p']/(1-r['push']) for r in records]);y=np.asarray([r['y'] for r in records])
    if not np.isfinite(p).all() or np.any(p<0) or np.any(p>1) or np.any((y!=0)&(y!=1)):
        raise ValueError('Invalid winner labels')
    weights=np.asarray([(week_weights or {}).get(int(r['kickoff']//604800),1.)/counts[r['fixture']] for r in records])
    if not np.isfinite(weights).all() or np.any(weights<=0):raise ValueError('Invalid winner weights')
    weights/=weights.sum();x=logit(np.clip(p,1e-12,1-1e-12))
    def gradient(bias):return float(weights@(expit(x+bias)-y)+2*PENALTY*bias)
    bias=-2. if gradient(-2.)>=0 else 2. if gradient(2.)<=0 else float(brentq(gradient,-2.,2.))
    return dict(meta,fitted=True,bias=bias,maxTrainingKickoff=max(r['kickoff'] for r in records))


def bank(records,cutoff):
    rows=[r for r in records if r['kickoff']+LABEL_LAG<=cutoff and not r['actualPush']]
    weeks=sorted({int(r['kickoff']//604800) for r in rows});rng=np.random.default_rng(SEED)
    return dict(point=fit(rows,cutoff),replicas=[fit(rows,cutoff,dict(zip(weeks,rng.exponential(size=len(weeks)).tolist()))) for _ in range(12)],
        uncertainty='Twelve seven-day reweights; sensitivity only, not calibrated lower coverage')


def decide(winner,parameters,conservative=False):
    if not parameters['point']['fitted']:return None
    point=adjusted(winner['p'],winner['push'],parameters['point']['bias'])
    p=min(winner['p'],point)
    if conservative:
        p=min(p,float(np.quantile([adjusted(winner['p'],winner['push'],v['bias']) for v in parameters['replicas']],.1)))
    ev=p*winner['odd']+winner['push']-1
    if not 1.8<winner['odd']<3 or p<.6 or ev<=0:return None
    confidence=min(winner['confidence'],p)
    # No alternative contracts or labels are available to this decision.
    result={k:v for k,v in winner.items() if k not in ('y','actualPush')}
    return dict(result,p=p,confidence=confidence,ev=ev,
        tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING')
