"""Joint corner counts from prior histories and other-market prices.

NB total with match-dependent dispersion and beta-binomial home allocation.
Frozen baseline means are offsets. No corner odds, ROI fit or model promotion.
"""
from collections import Counter
import math
import numpy as np
from scipy.optimize import minimize
from scipy.special import expit,gammaln,logit
from scipy.stats import nbinom,betabinom
from betting_v2.paired_count import from_means

FEATURES=('bias','log_goal_total','goal_imbalance','draw_residual','recent_corner_ratio','venue_corner_ratio','log_sot_level','sot_quality')
PENALTY=.01
MIN_FIXTURES=100
REPLICAS=12
SEED=18102026

def feature_vector(anchor,base_means,context):
    base=np.asarray(base_means,dtype=float)
    if base.shape!=(2,) or not np.isfinite(base).all() or np.any(base<=0):raise ValueError('Positive baseline corner means required')
    if not(anchor['featureCutoff']==context['featureCutoff']<=anchor['observed']-86400<anchor['kickoff']):raise ValueError('Corner features cross quote cutoff')
    gh,ga=anchor['home'],anchor['away']
    if not all(math.isfinite(v) and v>0 for v in (gh,ga)):raise ValueError('Invalid implied goal intensities')
    total=float(base.sum());imbalance=abs(gh-ga)/(gh+ga);quality=min(1.,min(context['sot_home'],context['sot_away'])/20.)
    def ratio(value,reference):
        if not math.isfinite(value) or value<=0:raise ValueError('Nonpositive historical count feature')
        return float(np.clip(math.log(value/reference),-1.5,1.5))
    x=[1.,ratio(gh+ga,2.6),imbalance,float(np.clip(anchor['drawResidual'],-.3,.3)),ratio(context['corner_recent'],total),
       ratio(context['corner_venue'],total),ratio(context['sot_recent'],8.)*quality,quality]
    if not np.isfinite(x).all():raise ValueError('Nonfinite corner design')
    return dict(x=x,allocation=[1.,(gh-ga)/(gh+ga)],dispersion=[1.,imbalance],baseMeans=base.tolist())

def design(records):
    x=np.asarray([r['x'] for r in records],dtype=float);z=np.asarray([r['allocation'] for r in records],dtype=float)
    d=np.asarray([r['dispersion'] for r in records],dtype=float);means=np.asarray([r['baseMeans'] for r in records],dtype=float)
    y=np.asarray([r['actual'] for r in records],dtype=float)
    if x.shape!=(len(records),len(FEATURES)) or z.shape!=(len(records),2) or d.shape!=(len(records),2):raise ValueError('Invalid corner feature dimensions')
    if means.shape!=(len(records),2) or y.shape!=(len(records),2):raise ValueError('Paired means and counts required')
    if any(not np.isfinite(a).all() for a in (x,z,d,means,y)) or np.any(means<=0) or np.any(y<0) or np.any(y!=np.floor(y)):raise ValueError('Invalid observed corner counts')
    return x,z,d,means,y

def objective(theta,arrays,weights):
    """Stable exact joint log likelihood with analytic gradient."""
    x,z,d,means,y=arrays;kx=len(FEATURES);mu=means.sum(axis=1)*np.exp(x@theta[:kx])
    alpha=np.exp(d@theta[kx:kx+2]);k=np.exp(theta[-1]);p=expit(logit(means[:,0]/means.sum(axis=1))+z@theta[kx+2:kx+4])
    n=y.sum(axis=1);h=y[:,0];a=y[:,1];steps=np.arange(int(n.max(initial=0)))[None,:]
    nn=n[:,None]>steps;hn=h[:,None]>steps;an=a[:,None]>steps;aa=alpha[:,None];ph=p[:,None]*k;pa=(1-p[:,None])*k
    total_ll=n*np.log(mu)-gammaln(n+1)+(np.log1p(steps*aa)*nn).sum(axis=1)-(1/alpha+n)*np.log1p(alpha*mu)
    allocation_ll=(gammaln(n+1)-gammaln(h+1)-gammaln(a+1)+h*np.log(p)+a*np.log1p(-p)
        +(np.log1p(steps/ph)*hn).sum(axis=1)+(np.log1p(steps/pa)*an).sum(axis=1)-(np.log1p(steps/k)*nn).sum(axis=1))
    dmean=(n-mu)/(1+alpha*mu)
    dalpha=(steps*aa/(1+steps*aa)*nn).sum(axis=1)+np.log1p(alpha*mu)/alpha-(1/alpha+n)*alpha*mu/(1+alpha*mu)
    ds=h*(1-p)-a*p-(1-p)*(steps/(ph+steps)*hn).sum(axis=1)+p*(steps/(pa+steps)*an).sum(axis=1)
    dk=-(steps/(ph+steps)*hn).sum(axis=1)-(steps/(pa+steps)*an).sum(axis=1)+(steps/(k+steps)*nn).sum(axis=1)
    gradient=-np.r_[x.T@(weights*dmean),d.T@(weights*dalpha),z.T@(weights*ds),weights@dk]
    mask=np.ones(len(theta));mask[kx]=0.;mask[-1]=0.
    return -float(weights@(total_ll+allocation_ll))+PENALTY*float((mask*theta)@theta),gradient+2*PENALTY*mask*theta

def fit(records,cutoff,dynamic=True,week_weights=None):
    if any(r['kickoff']+86400>cutoff or r['featureCutoff']>=r['kickoff'] for r in records):raise ValueError('Corner labels/features cross prior boundary')
    counts=Counter(r['fixture'] for r in records);kx=len(FEATURES)
    meta=dict(fixtures=len(counts),rows=len(records),trainingBefore=cutoff,labelLagSeconds=86400,features=list(FEATURES),
        dynamicDispersion=dynamic,penalty=PENALTY,roiUsed=False,objective='Prior fixture-weighted joint NB / beta-binomial log loss')
    if len(counts)<MIN_FIXTURES:return dict(meta,fitted=False,reason='INSUFFICIENT_PRIOR_FIXTURES')
    arrays=design(records);weights=np.asarray([(week_weights or {}).get(int(r['kickoff']//604800),1.)/counts[r['fixture']] for r in records])
    if not np.isfinite(weights).all() or np.any(weights<=0):raise ValueError('Invalid training weights')
    weights/=weights.sum();theta=np.r_[np.zeros(kx),math.log(.05),0.,0.,0.,math.log(30.)]
    bounds=[(-.7,.7)]+[(-1.,1.)]*(kx-1)+[(-9.,math.log(.5)),(-1.,1.) if dynamic else (0.,0.),(-1.,1.),(-1.,1.),(-1.,12.)]
    result=minimize(objective,theta,args=(arrays,weights),jac=True,method='L-BFGS-B',bounds=bounds,
                    options=dict(maxiter=500,ftol=1e-11,gtol=1e-7,maxls=50))
    if not result.success or not np.isfinite(result.fun):raise ValueError('Corner fitting failed '+str(result.message))
    return dict(meta,fitted=True,coef=result.x.tolist(),loss=float(result.fun),maxTrainingKickoff=max(r['kickoff'] for r in records),optimizerIterations=int(result.nit))

def bank(frame,cutoff,dynamic=True):
    prior=[r for r in frame if r['kickoff']+86400<=cutoff];weeks=sorted({int(r['kickoff']//604800) for r in prior});rng=np.random.default_rng(SEED)
    return dict(point=fit(prior,cutoff,dynamic),replicas=[fit(prior,cutoff,dynamic,dict(zip(weeks,rng.exponential(size=len(weeks)).tolist()))) for _ in range(REPLICAS)],
                uncertainty='Twelve weekly-weight sensitivities, not a calibrated confidence bound')

def parameters(record,model):
    if not model.get('fitted'):raise ValueError('Unfitted corner model')
    t=np.asarray(model['coef']);kx=len(FEATURES);base=np.asarray(record['baseMeans']);x=np.asarray(record['x']);z=np.asarray(record['allocation']);d=np.asarray(record['dispersion'])
    if t.shape!=(kx+5,) or base.shape!=(2,) or np.any(base<=0) or not np.isfinite(t).all():raise ValueError('Invalid corner parameter state')
    total=float(base.sum()*np.exp(x@t[:kx]));share=float(expit(logit(base[0]/base.sum())+z@t[kx+2:kx+4]))
    alpha=float(np.exp(d@t[kx:kx+2]));concentration=float(np.exp(t[-1]))
    if not 0<total<200 or not 0<share<1:raise ValueError('Corner prediction outside numerical support')
    return dict(home=total*share,away=total*(1-share),alpha=alpha,concentration=concentration)

def distribution(record,model):return from_means(**parameters(record,model))

def joint_log_probability(record,model):
    p=parameters(record,model);home,away=record['actual'];total=home+away;mean=p['home']+p['away'];k=p['concentration'];share=p['home']/mean
    return float(nbinom.logpmf(total,1/p['alpha'],1/(1+p['alpha']*mean))+betabinom.logpmf(home,total,share*k,(1-share)*k))
