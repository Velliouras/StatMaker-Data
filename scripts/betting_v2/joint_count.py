"""Exact, coherent market probabilities; no simulation threshold noise."""
from dataclasses import dataclass
import math
import numpy as np
from scipy.stats import poisson, nbinom, binom

VERSION='betting-v2-joint-count-v4'
TAIL=1e-12

@dataclass
class JointCount:
    pmf: np.ndarray
    omitted_mass: float=0.

    def __post_init__(self):
        self.pmf=np.asarray(self.pmf,dtype=float)
        if self.pmf.ndim!=2 or not np.isfinite(self.pmf).all() or (self.pmf<0).any() or self.pmf.sum()<=0:
            raise ValueError('Invalid probability distribution')
        self.pmf=self.pmf/self.pmf.sum()

    @classmethod
    def from_means(cls,home,away,*,scale=1.,alpha=0.,draw_tilt=0.,btts_tilt=0.):
        if not all(math.isfinite(v) for v in (home,away,scale,alpha,draw_tilt,btts_tilt)) or min(home,away,scale)<=0 or alpha<0:
            raise ValueError('Invalid count parameters')
        vectors=[];tails=[]
        for mean in (home*scale,away*scale):
            rv=poisson(mean) if alpha<1e-7 else nbinom(1/alpha,1/(1+alpha*mean))
            end=int(rv.ppf(1-TAIL))
            if end>1000:raise ValueError('Count tail exceeds precision budget')
            p=rv.pmf(np.arange(end+1));vectors.append(p);tails.append(max(0.,1-float(p.sum())))
        joint=np.outer(*vectors);h,a=np.indices(joint.shape)
        exponent=draw_tilt*(h==a)+btts_tilt*((h>0)&(a>0))
        joint*=np.exp(exponent-exponent.max())
        return cls(joint,min(1.,sum(tails)*math.exp(abs(draw_tilt)+abs(btts_tilt))))

    @property
    def means(self):
        h,a=np.indices(self.pmf.shape)
        return float((h*self.pmf).sum()),float((a*self.pmf).sum())

    def masks(self,spec):
        _,scope,side,team,line=spec
        allowed={'RESULT':('HOME','DRAW','AWAY'),'DC':('HOME_OR_DRAW','AWAY_OR_DRAW','HOME_OR_AWAY'),
            'DNB':('HOME','AWAY'),'BTTS':('YES','NO'),'TOTAL':('OVER','UNDER'),'TEAM':('OVER','UNDER')}
        if scope not in allowed or side not in allowed[scope]:raise ValueError('Unknown market')
        h,a=np.indices(self.pmf.shape);push=np.zeros_like(h,dtype=bool)
        if scope=='RESULT':win={'HOME':h>a,'DRAW':h==a,'AWAY':h<a}[side]
        elif scope=='DC':win={'HOME_OR_DRAW':h>=a,'AWAY_OR_DRAW':h<=a,'HOME_OR_AWAY':h!=a}[side]
        elif scope=='DNB':win=h>a if side=='HOME' else a>h;push=h==a
        elif scope=='BTTS':
            win=(h>0)&(a>0)
            if side=='NO':win=~win
        else:
            if line is None or not math.isfinite(line) or line<0 or line*2!=int(line*2):
                raise ValueError('Only integer and half lines supported')
            if scope=='TOTAL' and team not in (None,''):raise ValueError('Unexpected total side')
            if scope=='TEAM' and team not in ('HOME','AWAY'):raise ValueError('Missing team side')
            value=h if team=='HOME' else a if team=='AWAY' else h+a
            win=value>line if side=='OVER' else value<line;push=value==line
        return win&~push,push

    def evaluate(self,spec):
        win,push=self.masks(spec)
        return float(self.pmf[win].sum()),float(self.pmf[push].sum())

    def condition(self,side):
        if side not in ('HOME','AWAY'):raise ValueError('Unknown side')
        h,a=np.indices(self.pmf.shape);mask=h>a if side=='HOME' else a>h
        mass=float(self.pmf[mask].sum())
        return (JointCount(self.pmf*mask,self.omitted_mass/mass),mass) if mass>TAIL else (None,mass)

    def thin(self,home_share,away_share):
        if not all(math.isfinite(x) and 0<=x<=1 for x in (home_share,away_share)):raise ValueError('Invalid period share')
        nh,na=self.pmf.shape
        ht=binom.pmf(np.arange(nh)[None,:],np.arange(nh)[:,None],home_share)
        at=binom.pmf(np.arange(na)[None,:],np.arange(na)[:,None],away_share)
        return JointCount(ht.T@self.pmf@at,self.omitted_mass)


def outcome(home,away,spec):
    if not all(math.isfinite(v) and v>=0 and int(v)==v for v in (home,away)):
        raise ValueError('Invalid observed counts')
    mass=np.zeros((int(home)+1,int(away)+1));mass[int(home),int(away)]=1.
    return JointCount(mass).evaluate(spec)


def goal_periods(full,home_share,away_share):
    dist={'GOALS_FT':full,'GOALS_1H':full.thin(home_share,away_share),
          'GOALS_2H':full.thin(1-home_share,1-away_share)}
    scenarios={}
    for side in ('HOME','AWAY'):
        conditional,mass=full.condition(side)
        for metric in dist:
            value=conditional
            if value is not None and metric!='GOALS_FT':
                sh,sa=(home_share,away_share) if metric=='GOALS_1H' else (1-home_share,1-away_share)
                value=value.thin(sh,sa)
            scenarios[metric,side]=(value,mass)
    return dist,scenarios
