"""Coupled match counts: total tempo and allocation between the two teams.

N follows a negative binomial and H|N a beta-binomial; A=N-H. This can
represent positive or negative dependence, unlike independent team marginals.
"""
from collections import defaultdict
import numpy as np
from scipy.optimize import minimize
from scipy.stats import nbinom,poisson,betabinom
from scipy.special import gammaln
from .joint_count import JointCount


def from_means(home,away,*,scale=1.,alpha=0.,concentration=None):
    mean=(home+away)*scale;p=home/(home+away)
    if not 0<mean or not 0<p<1 or alpha<0:raise ValueError('Invalid paired count means')
    if concentration is None:
        if alpha!=0:raise ValueError('Poisson fallback must have zero dispersion')
        return JointCount.from_means(home,away,scale=scale)
    if not np.isfinite(concentration) or concentration<=0:raise ValueError('Invalid count allocation')
    total=poisson(mean) if alpha<1e-6 else nbinom(1/alpha,1/(1+alpha*mean))
    end=max(1,int(total.ppf(1-1e-10)))
    if end>1000:raise ValueError('Paired count support exceeds safe numerical bound')
    h,a=np.indices((end+1,end+1));n=h+a;valid=n<=end;pmf=np.zeros_like(n,dtype=float)
    pmf[valid]=total.pmf(n[valid])*betabinom.pmf(h[valid],n[valid],p*concentration,(1-p)*concentration)
    return JointCount(pmf,float(total.sf(end)))


def fit_profile(rows):
    grouped=defaultdict(list)
    for r in rows:grouped[r['metric'],r['regime']].append(r)
    result={}
    for (metric,regime),sample in grouped.items():
        base=dict(family='total_allocation',scale=1.,alpha=0.,concentration=None,n=len(sample),fitted=False)
        if len(sample)<100:result[metric+'::'+regime]=base;continue
        means=np.asarray([r['means'] for r in sample]);actual=np.asarray([r['actual'] for r in sample])
        total_mean=means.sum(axis=1);share=means[:,0]/total_mean;total_actual=actual.sum(axis=1);home=actual[:,0]
        # Rising-factorial log1p forms avoid cancellation near the Poisson and
        # binomial limits. Analytic derivatives avoid noisy finite differences.
        steps=np.arange(int(total_actual.max()))[None,:]
        hn=home[:,None]>steps;an=(total_actual-home)[:,None]>steps;nn=total_actual[:,None]>steps
        base_allocation=(gammaln(total_actual+1)-gammaln(home+1)-gammaln(total_actual-home+1)
            +home*np.log(share)+(total_actual-home)*np.log1p(-share))
        def loss(theta):
            scale,alpha,k=np.exp(theta);mu=total_mean*scale
            rising=(np.log1p(steps*alpha)*nn).sum(axis=1)
            total_ll=total_actual*np.log(mu)-gammaln(total_actual+1)+rising-(1/alpha+total_actual)*np.log1p(alpha*mu)
            kh=share[:,None]*k;ka=(1-share[:,None])*k
            allocation=(base_allocation+(np.log1p(steps/kh)*hn).sum(axis=1)
                +(np.log1p(steps/ka)*an).sum(axis=1)-(np.log1p(steps/k)*nn).sum(axis=1))
            dscale=(total_actual-mu)/(1+alpha*mu)
            dalpha=((steps*alpha/(1+steps*alpha)*nn).sum(axis=1)+np.log1p(alpha*mu)/alpha
                -(1/alpha+total_actual)*alpha*mu/(1+alpha*mu))
            dk=(-(steps/(kh+steps)*hn).sum(axis=1)-(steps/(ka+steps)*an).sum(axis=1)
                +(steps/(k+steps)*nn).sum(axis=1))
            return -float((total_ll+allocation).mean()),-np.asarray([dscale.mean(),dalpha.mean(),dk.mean()])
        fit=minimize(loss,[0.,np.log(.05),np.log(30.)],jac=True,method='L-BFGS-B',bounds=[(-.7,.7),(-12.,2.),(-3.,14.)],options=dict(maxiter=500,ftol=1e-10,gtol=1e-7,maxls=50))
        if not fit.success or not np.isfinite(fit.fun):raise ValueError('Paired count calibration failed '+str(fit.message))
        scale,alpha,k=map(float,np.exp(fit.x))
        result[metric+'::'+regime]=dict(base,scale=scale,alpha=alpha,concentration=k,fitted=True,loss=float(fit.fun))
    return result
