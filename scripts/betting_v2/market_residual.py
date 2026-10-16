"""Regularized proper-score learning of deviations from market probabilities.

Unlike a convex pool, the statistical deviation can receive a negative
coefficient if prior data support it. Complete partitions stay normalized.
"""
from collections import Counter,defaultdict
import numpy as np
from scipy.optimize import minimize
from .market_calibration import partition,SIDES

MIN_FIXTURES=30
PENALTY=.01
BOUNDS=((-1.,1.),(-.5,.5))


def groups(rows,metric=None,cutoff=None,*,for_training=True):
    grouped=defaultdict(list)
    for index,r in enumerate(rows):
        p=partition(r['spec'])
        if p is None or not r.get('reference') or r['push']>=1:continue
        if metric is not None and r['spec'][0]!=metric:continue
        if cutoff is not None and r['kickoff']>=cutoff:continue
        key=r['fixture'],r['sourceReceipt'],r['bookmaker'],p
        grouped[key].append((index,r))
    result=[]
    for key,items in grouped.items():
        expected=SIDES[key[3][1]];by={}
        for i,r in items:
            side=r['spec'][2]
            if side in by:by[side]=None
            else:by[side]=(i,r)
        if set(by)!=set(expected) or any(v is None for v in by.values()):continue
        ordered=[by[s] for s in expected]
        actual_push=any(r['actualPush'] for _,r in ordered)
        if for_training and actual_push:continue
        y=np.asarray([r['y'] for _,r in ordered])
        if not actual_push and abs(y.sum()-1)>1e-12:raise ValueError('Partition outcomes are not mutually exclusive')
        ps=np.asarray([r['p']/(1-r['push']) for _,r in ordered]);pm=np.asarray([r['reference']['q'] for _,r in ordered])
        if abs(ps.sum()-1)>1e-8 or abs(pm.sum()-1)>1e-8:raise ValueError('Partition probabilities are not normalized')
        result.append(dict(key=key,fixture=key[0],metric=key[3][0],kickoff=ordered[0][1]['kickoff'],
            indexes=[i for i,_ in ordered],sides=expected,stat=ps.tolist(),market=pm.tolist(),y=y.tolist()))
    return result


def fit_residual(partitions,cutoff):
    if any(r['kickoff']>=cutoff for r in partitions):raise ValueError('Residual calibration crosses prediction cutoff')
    counts=Counter(r['fixture'] for r in partitions);n=len(counts)
    metadata=dict(fixtures=n,partitions=len(partitions),trainingBefore=cutoff,penalty=PENALTY,
        objective='Prior fixture-weighted multiclass log loss',roiUsed=False)
    if n<MIN_FIXTURES:return dict(metadata,coef=[0.,0.],fitted=False,reason='INSUFFICIENT_PRIOR_FIXTURES_USE_MARKET')
    logq=np.full((len(partitions),3),-1e6);logp=logq.copy();target=np.zeros_like(logq)
    for i,r in enumerate(partitions):
        k=len(r['stat']);logq[i,:k]=np.log(np.maximum(r['market'],1e-12));logp[i,:k]=np.log(np.maximum(r['stat'],1e-12));target[i,:k]=r['y']
    deviation=logp-logq;weights=np.asarray([1/counts[r['fixture']] for r in partitions]);weights/=weights.sum()
    def objective(theta):
        z=logq+theta[0]*deviation+theta[1]*logq;z-=z.max(axis=1,keepdims=True)
        p=np.exp(z);p/=p.sum(axis=1,keepdims=True);error=weights[:,None]*(p-target)
        loss=-float(weights@np.sum(target*np.log(np.maximum(p,1e-15)),axis=1))+PENALTY*float(theta@theta)
        gradient=np.asarray([np.sum(error*deviation),np.sum(error*logq)])+2*PENALTY*theta
        return loss,gradient
    fit=minimize(objective,[0.,0.],jac=True,bounds=BOUNDS,method='L-BFGS-B',options=dict(ftol=1e-12,gtol=1e-8,maxiter=500))
    if not fit.success:raise ValueError('Residual calibration failed '+str(fit.message))
    return dict(metadata,coef=fit.x.tolist(),fitted=True,reason='FITTED_PRIOR_LOG_LOSS',loss=float(fit.fun),
        maxTrainingKickoff=max(r['kickoff'] for r in partitions))


def probabilities(stat,market,coef):
    ps=np.asarray(stat);pm=np.asarray(market);theta=np.asarray(coef)
    if theta.shape!=(2,) or not np.isfinite(theta).all():raise ValueError('Invalid residual coefficients')
    if np.any(ps<0) or np.any(pm<0) or abs(ps.sum()-1)>1e-8 or abs(pm.sum()-1)>1e-8:raise ValueError('Invalid probability partition')
    lp=np.log(np.maximum(ps,1e-12));lq=np.log(np.maximum(pm,1e-12))
    z=lq+theta[0]*(lp-lq)+theta[1]*lq;z-=z.max();p=np.exp(z);return p/p.sum()
