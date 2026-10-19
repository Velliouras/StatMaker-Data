"""Prior-only logistic market residual with a nonnegative statistical weight."""
from collections import Counter
import numpy as np
from scipy.special import expit,logit
from scipy.optimize import minimize
MIN_FIXTURES=30
PENALTY=.01
SEED=19102026


def fit(rows,cutoff,week_weights=None):
    if any(r['kickoff']+86400>cutoff or r['featureCutoff']>=r['kickoff'] for r in rows):raise ValueError('Residual fit crosses prior boundary')
    counts=Counter(r['fixture'] for r in rows);meta=dict(fixtures=len(counts),events=len(rows),trainingBefore=cutoff,labelLagSeconds=86400,roiUsed=False,penalty=PENALTY,objective='Prior fixture-weighted conditional log loss with market logit offset',parameters=['market_bias','nonnegative_stat_logit_weight'])
    if len(counts)<MIN_FIXTURES:return dict(meta,fitted=False,theta=[0.,0.],reason='INSUFFICIENT_PRIOR_FIXTURES')
    ps=np.clip([r['statOver'] for r in rows],1e-12,1-1e-12);pm=np.asarray([r['marketOver'] for r in rows]);y=np.asarray([r['yOver'] for r in rows])
    if not np.isfinite(ps).all() or not np.isfinite(pm).all() or np.any((pm<=0)|(pm>=1)) or np.any((y!=0)&(y!=1)):raise ValueError('Invalid residual input')
    offset=logit(pm);gap=logit(ps)-offset;weights=np.asarray([(week_weights or {}).get(int(r['kickoff']//604800),1.)/counts[r['fixture']] for r in rows]);
    if not np.isfinite(weights).all() or np.any(weights<=0):raise ValueError('Invalid prior weights')
    weights/=weights.sum()
    def objective(theta):
        z=offset+theta[0]+theta[1]*gap;e=expit(z)-y
        return float(weights@(np.logaddexp(0,z)-y*z)+PENALTY*(theta@theta)),np.array([weights@e,weights@(e*gap)])+2*PENALTY*theta
    f=minimize(objective,np.zeros(2),jac=True,method='L-BFGS-B',bounds=[(-2.,2.),(0.,1.)],options=dict(ftol=1e-12,gtol=1e-8,maxiter=500))
    if not f.success or not np.isfinite(f.fun):raise ValueError('Market residual fit failed '+str(f.message))
    return dict(meta,fitted=True,theta=f.x.tolist(),loss=float(f.fun),maxTrainingKickoff=max(r['kickoff'] for r in rows))


def bank(frame,metric,cutoff):
    rows=[r for r in frame if r['metric']==metric and r['kickoff']+86400<=cutoff];weeks=sorted({int(r['kickoff']//604800) for r in rows});rng=np.random.default_rng(SEED)
    return dict(point=fit(rows,cutoff),replicas=[fit(rows,cutoff,dict(zip(weeks,rng.exponential(size=len(weeks)).tolist()))) for _ in range(12)],uncertainty='Weekly sensitivity, not a calibrated confidence guarantee')


def predict(stat,market,theta):
    if len(theta)!=2 or not np.isfinite([stat,market,*theta]).all() or not 0<=stat<=1 or not 0<market<1 or not -2<=theta[0]<=2 or not 0<=theta[1]<=1:raise ValueError('Invalid residual probability')
    return float(expit(logit(market)+theta[0]+theta[1]*(logit(np.clip(stat,1e-12,1-1e-12))-logit(market))))


def candidate(q,reference,parameters,kind='residual'):
    if kind not in ('market','residual'):raise ValueError('Unknown residual decision kind')
    side=q['spec'][2];raw=q['p']/(1-q['push']);stat=raw if side=='OVER' else 1-raw;market=reference['q'] if side=='OVER' else 1-reference['q']
    theta=[0.,0.] if kind=='market' else parameters['point']['theta'];prob=predict(stat,market,theta);win=(prob if side=='OVER' else 1-prob)*(1-q['push']);p=min(q['p'],win);ev=p*q['odd']+q['push']-1
    if p<.6 or not 1.8<q['odd']<3 or ev<=0:return None
    lower=q['_lower'];low=lower['p']/(1-q['push']);ls=low if side=='OVER' else 1-low
    replicas=[dict(theta=theta)] if kind=='market' else parameters['replicas'];mapped=[]
    for f in replicas:
        v=predict(ls,market,f['theta']);mapped.append((v if side=='OVER' else 1-v)*(1-q['push']))
    point=predict(ls,market,theta);mapped_point=(point if side=='OVER' else 1-point)*(1-q['push'])
    confidence=min(q['confidence'],p)
    return dict(q,p=p,ev=ev,confidence=confidence,tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING',
        _lower=dict(lower,p=min(lower['p'],p,mapped_point,float(np.quantile(mapped,.1)))),marketReference=reference,residualWinProbability=win,uncalibratedPointP=q['p'])
