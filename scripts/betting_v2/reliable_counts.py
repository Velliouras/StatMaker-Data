"""Sample-aware, time-weighted count cores with strictly prior goal stacking.

Each metric has its own coefficients, priors and weekly sensitivity refits.
No odds, result-price calibration or cross-metric label substitution is used.
"""
import copy
import math
from itertools import groupby
import numpy as np
from .reliable_goals import ReliableGoals, INCREASING

METRICS = ('CORNERS','SHOTS','SOT','FOULS','YELLOW_CARDS','CARDS','RED_CARDS')
CEILINGS = dict(CORNERS=32.,SHOTS=65.,SOT=28.,FOULS=55.,YELLOW_CARDS=16.,CARDS=18.,RED_CARDS=8.)
COLD_PRIORS = dict(CORNERS=4.5,SHOTS=12.,SOT=4.,FOULS=12.,YELLOW_CARDS=2.,CARDS=2.,RED_CARDS=.08)


def metric_values(row, metric):
    if metric != 'RED_CARDS':
        import materialize_betting_v2_direct as direct
        return direct.metric_values(row,metric)
    stats=row.get('normalized_stats') or {}
    try:
        values=tuple(float(stats[k]) for k in ('HR','AR'))
        return values if all(math.isfinite(v) and v>=0 and v.is_integer() for v in values) else None
    except (KeyError,ValueError,TypeError):return None


def anchor_strength(state,team,opponent,group,opponent_group):
    if state['coldStart']:return 0.
    def effect(key):
        i=state['columns'].get(key)
        return state['coef'][i] if i is not None else 0.
    return (effect('attack:'+team)-effect('defence:'+team)+effect('league_attack:'+group)-effect('league_defence:'+group)
        -effect('attack:'+opponent)+effect('defence:'+opponent)-effect('league_attack:'+opponent_group)+effect('league_defence:'+opponent_group))


class ReliableCounts(ReliableGoals):
    def __init__(self,config):
        if config.get('metric') not in METRICS:raise ValueError('Explicit supported count metric required')
        super().__init__(dict(config,rateCeiling=CEILINGS[config['metric']]))
        self.increasing=(INCREASING|{'goal_strength_gap'}) if self.metric in ('CORNERS','SHOTS','SOT') else frozenset()
        self.bank=None;self.anchor=None;self._feature_row=None

    def prior(self,group,stat,venue=None):
        if stat!=self.metric:return super().prior(group,stat,venue)
        for key in ((group,stat,venue),(group,stat,None),('__GLOBAL__',stat,venue),('__GLOBAL__',stat,None)):
            values=self.priors.get(key)
            if values and values[2]>0:return values[0]/values[2],values[1]/values[2]
        return (COLD_PRIORS[stat],)*2

    def features(self,team,opponent,metric,home):
        f=super().features(team,opponent,metric,home)
        g,og=self.groups.get(team,'unmapped:'+team),self.groups.get(opponent,'unmapped:'+opponent)
        f.pop('league_attack:'+g);f.pop('league_defence:'+og)
        f['league_tempo:'+g]=.5;f['league_tempo:'+og]=f.get('league_tempo:'+og,0.)+.5
        if self._feature_row is not None:
            if self.bank is None:raise ValueError('Training needs chronological goal bank')
            r=self._feature_row
            strength=self.bank.strength(r,team,opponent,r['_home_group'],r['_away_group'],home)
        elif self.anchor is not None:
            strength=anchor_strength(self.anchor,team,opponent,g,og)
        else:raise ValueError('Count prediction needs frozen prior goal anchor')
        f['goal_strength_gap']=strength
        return f

    def observe(self,row,values):
        super().observe(row,values)
        target=values(row,self.metric)
        if target is None:return
        # The original observer does not contain this diagnostic metric.
        if self.metric=='RED_CARDS':
            for t,i,v in [(row['_home'],0,True),(row['_away'],1,False)]:
                self.history[t][self.metric].append((target[i],target[1-i],v))
        for side,venue,i in [('home',True,0),('away',False,1)]:
            group=row['_'+side+'_group']
            for g in (group,'__GLOBAL__'):
                for v in (None,venue):
                    p=self.priors[g,self.metric,v]
                    p[0]+=target[i];p[1]+=target[1-i];p[2]+=1

    def design(self,history,cutoff,values,*,bank):
        self.bank=bank
        result=super().design(history,cutoff,values)
        # This anchor is fitted strictly before every calibration/test fixture.
        if history:
            r=dict(history[0],_date=cutoff)
            bank.strength(r,r['_home'],r['_away'],r['_home_group'],r['_away_group'],True)
            from datetime import datetime,timezone
            d=datetime.fromtimestamp(cutoff,timezone.utc)
            key=datetime(d.year,d.month,1,tzinfo=timezone.utc).timestamp()
            self.anchor=copy.deepcopy(bank.cache[key])
            if not self.anchor['coldStart'] and self.anchor['maxTrainingTimestamp']>=cutoff:
                raise ValueError('Goal anchor crosses training cutoff')
        self._feature_row=None;self.bank=None
        return result

    def export(self):
        state=super().export();state['countGoalAnchor']=copy.deepcopy(self.anchor)
        return state

    @classmethod
    def restore(cls,state):
        model=super().restore(state)
        model.anchor=copy.deepcopy(state['countGoalAnchor'])
        return model


def rolling_predictions(model,history,start,end):
    if model.clock>start:raise ValueError('Feature clock would move backwards')
    rows=[]
    for day,batch in groupby((r for r in history if start<=r['_date']<end),lambda r:int(r['_date']//86400)):
        batch=list(batch);model.clock=day*86400
        for r in batch:
            bank=model.predict_bank(r['_home'],r['_away']);actual=metric_values(r,model.metric)
            if bank is None or actual is None:continue
            rows.append(dict(fixture=f"{r['_date']}:{r['_home']}:{r['_away']}",date=r['_date'],metric=model.metric,
                regime='XG' if min(len(model.xg[r['_home']]),len(model.xg[r['_away']]))>=8 else 'ELO_NO_XG',
                means=bank[0].tolist(),bootstrapMeans=bank[1:].tolist(),actual=list(actual),
                samples=[len(model.history[r['_home']][model.metric]),len(model.history[r['_away']][model.metric])],
                featureCutoff=model.clock))
        for r in batch:model.observe(r,metric_values)
    model.clock=end
    return rows


class CountModels:
    def __init__(self,models):
        self.models=models
        first=next(iter(models.values()));self.history,self.xg=first.history,first.xg
    def predict(self,home,away,metric):
        return self.models[metric].predict(home,away,metric) if metric in self.models else None
    def predict_bank(self,home,away,metric):
        return self.models[metric].predict_bank(home,away) if metric in self.models else None
