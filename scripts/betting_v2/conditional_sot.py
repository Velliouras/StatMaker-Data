"""SOT conditional on latent SHOTS, with prior conversion and count likelihood.

The SHOTS parent is supplied frozen by the caller. No odds or ROI enter fits.
"""
from collections import Counter,defaultdict
import math
import numpy as np
from scipy.optimize import minimize
from scipy.special import expit,logit,gammaln
from scipy.stats import betabinom
from betting_v2.joint_count import JointCount

PRIOR_SHOTS=120.
PENALTY=.01
SEED=18102026
REPLICAS=12

def paired_actual(row,metric_values):
    shots=metric_values(row,'SHOTS');sot=metric_values(row,'SOT')
    if shots is None or sot is None:return None
    n=np.asarray(shots,dtype=float);y=np.asarray(sot,dtype=float)
    if n.shape!=(2,) or y.shape!=(2,) or not np.isfinite(n).all() or not np.isfinite(y).all() or np.any(n<0) or np.any(y<0) or np.any(n!=np.floor(n)) or np.any(y!=np.floor(y)) or np.any(y>n):
        raise ValueError('Invalid paired SHOTS/SOT actuals')
    return n.tolist(),y.tolist()

class ConversionHistory:
    """Prefix-only, venue-specific attack/defence conversion with fixed shrinkage."""
    def __init__(self,history,metric_values):
        self.events=[];seen=set();self.index=0;self.clock=-math.inf;self.last=-math.inf;self.team_groups={}
        self.team=defaultdict(lambda:np.zeros(2));self.group=defaultdict(lambda:np.zeros(2));self.global_=defaultdict(lambda:np.zeros(2))
        for row in sorted(history,key=lambda r:r['_date']):
            key=(row['_date'],row['_home'],row['_away'])
            if key in seen:raise ValueError('Duplicate historical fixture')
            seen.add(key);actual=paired_actual(row,metric_values)
            if actual is not None:self.events.append((row,actual))
    def advance(self,cutoff):
        if cutoff<self.clock or cutoff%86400:raise ValueError('Conversion clock must advance by complete UTC days')
        while self.index<len(self.events) and self.events[self.index][0]['_date']<cutoff:
            r,(n,y)=self.events[self.index]
            for i,(side,venue) in enumerate((('home','HOME'),('away','AWAY'))):
                team=r['_'+side];group=r.get('_'+side+'_group','UNKNOWN');self.team_groups[team]=group
                for direction,j in (('FOR',i),('AGAINST',1-i)):
                    pair=np.asarray([y[j],n[j]]);self.team[team,venue,direction]+=pair
                    self.group[group,venue,direction]+=pair;self.global_[venue,direction]+=pair
            self.last=r['_date'];self.index+=1
        self.clock=cutoff
    def rate(self,team,group,venue,direction):
        def smooth(pair,prior):return float((pair[0]+PRIOR_SHOTS*prior)/(pair[1]+PRIOR_SHOTS))
        global_rate=smooth(self.global_[venue,direction],1/3)
        group_rate=smooth(self.group[group,venue,direction],global_rate)
        own=self.team[team,venue,direction]
        return smooth(own,group_rate),float(own[1])
    def features(self,home,away):
        home_group=self.team_groups.get(home,'UNKNOWN');away_group=self.team_groups.get(away,'UNKNOWN')
        probabilities=[];exposure=[]
        for own,opp,group,oppgroup,venue,oppvenue in [(home,away,home_group,away_group,'HOME','AWAY'),(away,home,away_group,home_group,'AWAY','HOME')]:
            p,n=self.rate(own,group,venue,'FOR');q,m=self.rate(opp,oppgroup,oppvenue,'AGAINST')
            probabilities.append(float(expit(.5*(logit(p)+logit(q)))));exposure.append([n,m])
        if self.last>=self.clock:raise ValueError('Conversion feature uses current/future label')
        return dict(baseConversion=probabilities,priorShots=exposure,featureCutoff=self.clock,maxObservedKickoff=self.last if math.isfinite(self.last) else None)

def design(records):
    n=np.asarray([r['shots'] for r in records],dtype=float).reshape(-1)
    y=np.asarray([r['sot'] for r in records],dtype=float).reshape(-1)
    base=np.asarray([r['baseConversion'] for r in records],dtype=float).reshape(-1)
    if any(a.shape!=(2*len(records),) or not np.isfinite(a).all() for a in (n,y,base)) or np.any(n<0) or np.any(y<0) or np.any(y>n) or np.any(n!=np.floor(n)) or np.any(y!=np.floor(y)) or np.any(base<=0) or np.any(base>=1):raise ValueError('Invalid conditional training design')
    x=np.tile(np.asarray([[1.,1.],[1.,-1.]]),(len(records),1))
    return n,y,logit(base),x

def objective(theta,arrays,weights):
    n,y,offset,x=arrays;p=expit(offset+x@theta[:2]);k=math.exp(theta[2]);fail=n-y
    steps=np.arange(int(n.max(initial=0)))[None,:];yn=y[:,None]>steps;fn=fail[:,None]>steps;nn=n[:,None]>steps
    alpha=p[:,None]*k;beta=(1-p[:,None])*k
    ll=gammaln(n+1)-gammaln(y+1)-gammaln(fail+1)+y*np.log(p)+fail*np.log1p(-p)
    ll+=(np.log1p(steps/alpha)*yn).sum(axis=1)+(np.log1p(steps/beta)*fn).sum(axis=1)-(np.log1p(steps/k)*nn).sum(axis=1)
    ds=y*(1-p)-fail*p-(1-p)*(steps/(alpha+steps)*yn).sum(axis=1)+p*(steps/(beta+steps)*fn).sum(axis=1)
    dk=-(steps/(alpha+steps)*yn).sum(axis=1)-(steps/(beta+steps)*fn).sum(axis=1)+(steps/(k+steps)*nn).sum(axis=1)
    penalty=PENALTY*float(theta[:2]@theta[:2]);grad=-np.r_[x.T@(weights*ds),weights@dk];grad[:2]+=2*PENALTY*theta[:2]
    return -float(weights@ll)+penalty,grad

def fit(records,cutoff,week_weights=None):
    if any(r['kickoff']+86400>cutoff or r['featureCutoff']>int(r['kickoff']//86400)*86400-86400 or (r['maxObservedKickoff'] is not None and r['maxObservedKickoff']>=r['featureCutoff']) for r in records):raise ValueError('Conditional fit crosses availability boundary')
    counts=Counter(r['fixture'] for r in records)
    if any(v!=1 for v in counts.values()):raise ValueError('Duplicate conditional training fixture')
    meta=dict(fixtures=len(counts),trainingBefore=cutoff,labelLagSeconds=86400,penalty=PENALTY,roiUsed=False,
        objective='Prior paired fixture-weighted beta-binomial conditional SOT log likelihood',features=['intercept','signed_home_venue'],priorShotMass=PRIOR_SHOTS)
    if len(counts)<100:return dict(meta,fitted=False,reason='INSUFFICIENT_PRIOR_FIXTURES')
    weights=np.repeat([(week_weights or {}).get(int(r['kickoff']//604800),1.) for r in records],2)
    if not np.isfinite(weights).all() or np.any(weights<=0):raise ValueError('Invalid conditional training weights')
    weights/=weights.sum();arrays=design(records)
    result=minimize(objective,np.asarray([0.,0.,math.log(30.)]),args=(arrays,weights),jac=True,method='L-BFGS-B',bounds=[(-1.,1.),(-1.,1.),(math.log(.5),math.log(10000.))],options=dict(maxiter=300,ftol=1e-11,gtol=1e-7,maxls=50))
    if not result.success or not np.isfinite(result.fun):raise ValueError('Conditional SOT fit failed '+str(result.message))
    return dict(meta,fitted=True,coef=result.x.tolist(),loss=float(result.fun),maxTrainingKickoff=max(r['kickoff'] for r in records),optimizerIterations=int(result.nit))

def bank(frame,cutoff):
    prior=[r for r in frame if r['kickoff']+86400<=cutoff];weeks=sorted({int(r['kickoff']//604800) for r in prior});rng=np.random.default_rng(SEED)
    return dict(point=fit(prior,cutoff),replicas=[fit(prior,cutoff,dict(zip(weeks,rng.exponential(size=len(weeks)).tolist()))) for _ in range(REPLICAS)],uncertainty='Twelve weekly-weight sensitivities with frozen SHOTS banks; not calibrated coverage')

def parameters(record,model):
    if not model.get('fitted'):raise ValueError('Unfitted conditional SOT model')
    t=np.asarray(model['coef']);p=np.asarray(record['baseConversion'])
    if not model.get('fitted') or t.shape!=(3,) or p.shape!=(2,) or not np.isfinite(t).all() or not np.isfinite(p).all() or np.any(p<=0) or np.any(p>=1):raise ValueError('Invalid conditional SOT parameter state')
    p=expit(logit(p)+t[0]+np.asarray([1.,-1.])*t[1]);return p,math.exp(t[2])

def transition(size,p,k):
    if not isinstance(size,int) or size<1 or not math.isfinite(p) or not 0<p<1 or not math.isfinite(k) or k<=0:raise ValueError('Invalid conditional transition')
    n=np.arange(size)[:,None];s=np.arange(size)[None,:];t=betabinom.pmf(s,n,p*k,(1-p)*k)
    t[s>n]=0.;t/=t.sum(axis=1,keepdims=True)
    if not np.isfinite(t).all():raise ValueError('Invalid SOT transition probability')
    return t

def distribution(parent,record,model):
    p,k=parameters(record,model);h,a=parent.pmf.shape
    return JointCount(transition(h,float(p[0]),k).T@parent.pmf@transition(a,float(p[1]),k),parent.omitted_mass)
