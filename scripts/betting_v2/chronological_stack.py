"""Offline chronological goal-strength stacking; production defaults unchanged."""
from collections import defaultdict
from datetime import datetime,timezone
from itertools import groupby
import math
import numpy as np
from scipy.sparse import csr_matrix
from sklearn.linear_model import LogisticRegression
from .opponent_matchup import OpponentMatchup
from .monotone_count import fit_monotone_count

def initialize(m):
    m.feature_exclusions=frozenset();m.use_goal_strength=True
    m.history=defaultdict(lambda:defaultdict(list));m.elo=defaultdict(lambda:1500.)
    m.xg=defaultdict(list);m.groups={};m.models={};m.columns={};m.feature_scales={}
    m.train=defaultdict(list);m.period_train=[];m.period_model=None;m.matches=0

def matrix(features):
    keys=sorted({k for f in features for k in f});columns={k:i for i,k in enumerate(keys)}
    rr,cc,vv=[],[],[]
    for i,values in enumerate(features):
        for k,v in values.items():
            if v:rr.append(i);cc.append(columns[k]);vv.append(v)
    x=csr_matrix((vv,(rr,cc)),shape=(len(features),len(keys)))
    avg=np.asarray(x.mean(axis=0)).ravel();var=np.asarray(x.power(2).mean(axis=0)).ravel()-avg**2
    scales=np.asarray([max(math.sqrt(max(0,var[i])),1e-4) if ':' not in k else 1. for i,k in enumerate(keys)])
    return x.multiply(1/scales).tocsr(),columns,scales

def fit_prior_goals(history,cutoff,metric_values):
    model=OpponentMatchup.__new__(OpponentMatchup);initialize(model);records=[]
    for _,batch in groupby((r for r in history if r['_date']<cutoff),lambda r:int(r['_date']//86400)):
        batch=list(batch)
        for r in batch:
            h,a=r['_home'],r['_away'];model.groups[h],model.groups[a]=r['_home_group'],r['_away_group']
            y=metric_values(r,'GOALS_FT')
            if y is not None:
                records.extend((model.features(t,o,'GOALS_FT',v),n) for t,o,v,n in ((h,a,True,y[0]),(a,h,False,y[1])))
        for r in batch:model.observe(r,metric_values)
    if len(records)<400:return dict(coldStart=True,trainingMatches=len(records)//2,cutoff=cutoff,coef=[],columns={})
    x,columns,_=matrix([f for f,_ in records]);fit=fit_monotone_count(x,np.asarray([y for _,y in records]),columns,'GOALS_FT',2./len(records))
    return dict(coldStart=False,trainingMatches=len(records)//2,cutoff=cutoff,
        maxTrainingTimestamp=max(r['_date'] for r in history if r['_date']<cutoff),coef=fit.coef_.tolist(),columns=columns)

class PriorGoalBank:
    def __init__(self,history,metric_values,on_fit=None):
        self.history=history;self.metric_values=metric_values;self.cache={};self.cold_fixtures=set();self.on_fit=on_fit
    def strength(self,row,team,opponent,home_group,away_group,venue):
        date=datetime.fromtimestamp(row['_date'],timezone.utc);cutoff=datetime(date.year,date.month,1,tzinfo=timezone.utc).timestamp()
        if cutoff not in self.cache:
            self.cache[cutoff]=fit_prior_goals(self.history,cutoff,self.metric_values)
            if self.on_fit:self.on_fit(self)
        state=self.cache[cutoff]
        if state['coldStart']:
            self.cold_fixtures.add((row['_date'],row['_home'],row['_away']));return 0.
        assert state['maxTrainingTimestamp']<state['cutoff']<=row['_date']
        def effect(k):
            i=state['columns'].get(k);return state['coef'][i] if i is not None else 0.
        g,og=(home_group,away_group) if venue else (away_group,home_group)
        return effect('attack:'+team)-effect('defence:'+team)+effect('league_attack:'+g)-effect('league_defence:'+g)-effect('attack:'+opponent)+effect('defence:'+opponent)-effect('league_attack:'+og)+effect('league_defence:'+og)

class ChronologicalStack(OpponentMatchup):
    def __init__(self,history,cutoff,metric_values,*,bank,goal_anchor=None,ridge_factor=1.):
        if not math.isfinite(ridge_factor) or ridge_factor<=0:
            raise ValueError('Regularization factor must be positive and finite')
        initialize(self);self.bank=bank;self.ridge_factor=ridge_factor
        metrics=('GOALS_FT','GOALS_1H','GOALS_2H','CORNERS','SHOTS','SOT','YELLOW_CARDS','CARDS','FOULS')
        for _,batch in groupby((r for r in history if r['_date']<cutoff),lambda r:int(r['_date']//86400)):
            batch=list(batch)
            for r in batch:
                h,a=r['_home'],r['_away'];hg,ag=r['_home_group'],r['_away_group'];self.groups[h],self.groups[a]=hg,ag
                targets={m:metric_values(r,m) for m in metrics}
                def features(t,o,m,v):
                    f=self.features(t,o,m,v)
                    if m!='GOALS_FT':f['_prior_goal_strength']=bank.strength(r,t,o,hg,ag,v)
                    return f
                for metric,target in targets.items():
                    if target is not None:
                        for t,o,v,y in ((h,a,True,target[0]),(a,h,False,target[1])):self.train[metric].append((features(t,o,metric,v),y))
                full,half=targets['GOALS_FT'],targets['GOALS_1H']
                if full is not None and half is not None:
                    for t,o,v,total,first in ((h,a,True,full[0],half[0]),(a,h,False,full[1],half[1])):
                        if total>0:
                            f=self.features(t,o,'GOALS_FT',v);f['_prior_goal_strength']=bank.strength(r,t,o,hg,ag,v)
                            self.period_train.append((f,first,total-first))
                self.matches+=1
            for r in batch:self.observe(r,metric_values)
        for metric in ['GOALS_FT']+[m for m in self.train if m!='GOALS_FT']:
            if metric in ('GOALS_1H','GOALS_2H'):continue
            if metric=='GOALS_FT' and goal_anchor is not None:
                from .live_exact import FrozenFit
                self.models[metric]=FrozenFit(goal_anchor)
                self.columns[metric]=goal_anchor['columns'];self.feature_scales[metric]=np.asarray(goal_anchor['scales'])
                continue
            records=self.train[metric]
            if not records:continue
            if metric!='GOALS_FT':records=[(self.count_strength_features(f),y) for f,y in records]
            x,columns,scales=matrix([f for f,_ in records]);self.columns[metric],self.feature_scales[metric]=columns,scales
            self.models[metric]=fit_monotone_count(x,np.asarray([y for _,y in records]),columns,metric,2.*ridge_factor/len(records))
        records=[]
        for f,first,second in self.period_train:
            values=self.count_strength_features(f)
            if first>0:records.append((values,1,first))
            if second>0:records.append((values,0,second))
        if len({y for _,y,_ in records})==2:
            x,self.period_columns,self.period_scales=matrix([f for f,_,_ in records])
            self.period_model=LogisticRegression(C=.5/ridge_factor,solver='newton-cholesky',max_iter=80,tol=1e-6).fit(x,[y for _,y,_ in records],sample_weight=[w for _,_,w in records])
    def count_strength_features(self,features):
        f=dict(features);prior=f.pop('_prior_goal_strength',None);result=super().count_strength_features(f)
        if prior is not None:result['goal_strength_gap']=prior
        return result
