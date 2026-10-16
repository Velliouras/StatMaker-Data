"""Chronological goal model with sample-aware priors and optional time weights.

Team effects shrink towards their league effects through an adaptive ridge.
This is a penalized GLM, not a claimed Bayesian posterior. Positive weekly
bootstrap weights measure training-parameter sensitivity, not guaranteed risk.
"""
from collections import defaultdict
import copy
from itertools import groupby
import math
import numpy as np
from scipy.optimize import minimize
from .opponent_matchup import OpponentMatchup
from .chronological_stack import initialize, matrix

CONFIG = dict(priorMatches=8., clubPriorMatches=20., ridgeFactor=50.,
              halfLifeDays=180., bootstrapReplicas=12, bootstrapSeed=16102026)
INCREASING = {'elo_gap','recent_for','recent_opponent_against','venue_for',
              'venue_opponent_against','xg_for','opponent_xg_against'}


def weighted_fit(x, y, weights, penalty, columns):
    weights = np.asarray(weights, float)
    if len(weights) != len(y) or not np.isfinite(weights).all() or np.any(weights <= 0):
        raise ValueError('Positive finite training weights required')
    weights = weights/weights.sum()
    start = np.zeros(x.shape[1]+1)
    start[0] = math.log(max(float(weights@y), 1e-6))
    constrained = {i for k,i in columns.items() if k in INCREASING}
    bounds = [(None,None)]+[(0.,None) if i in constrained else (None,None) for i in range(x.shape[1])]
    def objective(theta):
        eta = np.asarray(x@theta[1:]).ravel()+theta[0]
        if not np.isfinite(eta).all() or eta.max() > 600:
            return np.inf, np.zeros_like(theta)
        mu = np.exp(eta)
        error = weights*(mu-y)
        loss = float(weights@(mu-y*eta)+.5*np.sum(penalty*theta[1:]**2))
        grad = np.r_[error.sum(),np.asarray(x.T@error).ravel()+penalty*theta[1:]]
        return loss,grad
    result = minimize(objective,start,jac=True,bounds=bounds,method='L-BFGS-B',
                      options=dict(maxiter=2500,ftol=1e-11,gtol=1e-6,maxls=50))
    if not result.success or not np.isfinite(result.x).all():
        raise ValueError('Reliable goal fit failed: '+str(result.message))
    return dict(intercept=float(result.x[0]),coef=result.x[1:].tolist(),
                iterations=int(result.nit),loss=float(result.fun))


class ReliableGoals(OpponentMatchup):
    def __init__(self, config=None):
        initialize(self)
        self.config = dict(CONFIG,**(config or {}))
        self.priors = defaultdict(lambda:[0.,0.,0.])
        self.clock = 0.
        self.fits = []
        self.trainingBefore = None

    def prior(self, group, stat, venue=None):
        values = self.priors.get((group,stat,venue)) or self.priors.get((group,stat,None))
        if values and values[2] > 0:
            return values[0]/values[2],values[1]/values[2]
        # A feature prior, never a fabricated observed xG label.
        if stat == 'XG':
            return self.prior(group,'GOALS_FT',venue)
        return 1.25,1.25

    def smooth(self, values, prior):
        n = len(values); k = self.config['priorMatches']
        return (sum(values)+k*prior)/(n+k), n/(n+k)

    def features(self,team,opponent,metric,home):
        if metric != 'GOALS_FT':
            raise ValueError('Reliable goal core accepts FT goals only')
        t,o = self.history[team][metric],self.history[opponent][metric]
        tx,ox = self.xg[team],self.xg[opponent]
        g,og = self.groups.get(team,'unmapped:'+team),self.groups.get(opponent,'unmapped:'+opponent)
        tp,op = self.prior(g,metric),self.prior(og,metric)
        tvp,ovp = self.prior(g,metric,home),self.prior(og,metric,not home)
        xp,oxp = self.prior(g,'XG'),self.prior(og,'XG')
        recent,qr = self.smooth([v[0] for v in t[-8:]],tp[0])
        opponent_recent,qo = self.smooth([v[1] for v in o[-8:]],op[1])
        venue,qv = self.smooth([v[0] for v in t if v[2]==home][-8:],tvp[0])
        opponent_venue,qov = self.smooth([v[1] for v in o if v[2]!=home][-8:],ovp[1])
        xg,qx = self.smooth([v[0] for v in tx[-8:]],xp[0])
        opponent_xg,qox = self.smooth([v[1] for v in ox[-8:]],oxp[1])
        return {'attack:'+team:1.,'defence:'+opponent:1.,'league_attack:'+g:1.,'league_defence:'+og:1.,
                'home':float(home),'league_home:'+g:float(home),
                'elo_gap':(self.elo[team]-self.elo[opponent])/400.,
                'recent_for':recent,'recent_opponent_against':opponent_recent,
                'venue_for':venue,'venue_opponent_against':opponent_venue,
                'xg_for':xg,'opponent_xg_against':opponent_xg,
                'recent_reliability':qr,'opponent_reliability':qo,
                'venue_reliability':qv,'opponent_venue_reliability':qov,
                'xg_reliability':qx,'opponent_xg_reliability':qox,
                'xg_for_observed':float(bool(tx)),'opponent_xg_observed':float(bool(ox))}

    def observe(self,row,metric_values):
        super().observe(row,metric_values)
        goals = metric_values(row,'GOALS_FT')
        if goals is None:
            return
        stats = row.get('normalized_stats') or {}
        try:
            xg = tuple(float(stats[k]) for k in ('HxG','AxG'))
            if not all(math.isfinite(v) and v >= 0 for v in xg): xg = None
        except (TypeError,ValueError,KeyError):
            xg = None
        for side,venue,i in [('home',True,0),('away',False,1)]:
            group = row['_'+side+'_group']
            for stat,values in [('GOALS_FT',goals),('XG',xg)]:
                if values is None: continue
                for v in (None,venue):
                    prior = self.priors[group,stat,v]
                    prior[0] += values[i]; prior[1] += values[1-i]; prior[2] += 1

    def design(self,history,cutoff,metric_values):
        records,dates,teams = [],[],[]
        for day,batch in groupby((r for r in history if r['_date']<cutoff),lambda r:int(r['_date']//86400)):
            batch = list(batch);self.clock = day*86400
            for r in batch:
                h,a = r['_home'],r['_away'];self.groups[h],self.groups[a] = r['_home_group'],r['_away_group']
                y = metric_values(r,'GOALS_FT')
                if y is not None:
                    for t,o,v,value in [(h,a,True,y[0]),(a,h,False,y[1])]:
                        records.append((self.features(t,o,'GOALS_FT',v),value));dates.append(r['_date']);teams.append(t)
                    self.matches += 1
            for r in batch: self.observe(r,metric_values)
        if not records: raise ValueError('No prior training observations')
        x,columns,scales = matrix([f for f,_ in records])
        self.columns['GOALS_FT'],self.feature_scales['GOALS_FT'] = columns,scales
        self.trainingBefore = cutoff
        self.clock = cutoff
        self.train['GOALS_FT'] = range(len(records))
        return x,np.asarray([y for _,y in records]),np.asarray(dates),teams

    def fit_bank(self,design,recency=True,bootstrap=True,seed=None):
        x,y,dates,teams = design
        age = np.maximum(0.,(self.trainingBefore-dates)/86400)
        weights = np.exp(-math.log(2)*age/self.config['halfLifeDays']) if recency else np.ones(len(y))
        weighted_counts = defaultdict(float)
        for team,w in zip(teams,weights): weighted_counts[team] += float(w)
        columns = self.columns['GOALS_FT']
        # Adaptive team penalties centre club residuals on the league effects.
        penalty = np.full(len(columns),2*self.config['ridgeFactor']/len(y))
        for key,i in columns.items():
            if key.startswith(('attack:','defence:')):
                team = key.split(':',1)[1]
                penalty[i] *= math.sqrt(1+self.config['clubPriorMatches']/max(weighted_counts[team],1.))
        self.fits = [weighted_fit(x,y,weights,penalty,columns)]
        weeks = (dates//(7*86400)).astype(int);unique = np.unique(weeks)
        rng = np.random.default_rng(self.config['bootstrapSeed'] if seed is None else seed)
        for _ in range(self.config['bootstrapReplicas'] if bootstrap else 0):
            draws = dict(zip(unique,rng.exponential(1.,len(unique))))
            boot_weights = weights*np.asarray([max(draws[w],1e-8) for w in weeks])
            self.fits.append(weighted_fit(x,y,boot_weights,penalty,columns))
        self._compile()
        return dict(trainingMatches=self.matches,trainingRows=len(y),columns=len(columns),recency=recency,
            halfLifeDays=self.config['halfLifeDays'] if recency else None,
            effectiveFixtureWeights=float(weights.sum()/2),bootstrapReplicas=len(self.fits)-1,
            bootstrapMethod='Positive iid exponential weights on complete UTC-week blocks',
            trainingBefore=self.trainingBefore,maxTrainingTimestamp=float(dates.max()),
            featureQuality='Recent/venue/xG priors use preceding observations only',
            penalties=penalty.tolist(),fits=[{k:v for k,v in fit.items() if k!='coef'} for fit in self.fits])

    def _compile(self):
        self._coef = np.asarray([f['coef'] for f in self.fits])
        self._intercept = np.asarray([f['intercept'] for f in self.fits])

    def predict_bank(self,home,away):
        if not self.fits or not self.history[home]['GOALS_FT'] or not self.history[away]['GOALS_FT']:
            return None
        columns = self.columns['GOALS_FT'];scales = self.feature_scales['GOALS_FT']
        result = []
        for team,opp,venue in [(home,away,True),(away,home,False)]:
            values = self.features(team,opp,'GOALS_FT',venue)
            active = [(columns[k],v) for k,v in values.items() if k in columns and v]
            idx = [i for i,v in active];vals = np.asarray([v for i,v in active])/scales[idx]
            eta = self._intercept+(self._coef[:,idx]*vals).sum(axis=1)
            rates = np.exp(eta)
            if not np.isfinite(rates).all() or np.any(rates <= 0) or np.any(rates > 12):
                return None
            result.append(rates)
        return np.column_stack(result)

    def predict(self,home,away,metric):
        if metric != 'GOALS_FT':return None
        bank = self.predict_bank(home,away)
        return None if bank is None else (float(bank[0,0]),float(bank[0,1]),
            len(self.history[home][metric]),len(self.history[away][metric]),self.matches)

    def export(self):
        return copy.deepcopy(dict(schema=1,config=self.config,clock=self.clock,trainingBefore=self.trainingBefore,
            trainingMatches=self.matches,groups=self.groups,elo=dict(self.elo),
            history={t:dict(v) for t,v in self.history.items()},xg=dict(self.xg),
            priors=[[list(k),v] for k,v in self.priors.items()],
            columns=self.columns['GOALS_FT'],scales=self.feature_scales['GOALS_FT'].tolist(),fits=self.fits))

    @classmethod
    def restore(cls,state):
        if state['schema'] != 1:raise ValueError('Unknown reliable goal state')
        model = cls(state['config']);model.clock = state['clock'];model.trainingBefore = state['trainingBefore']
        model.matches = state['trainingMatches'];model.groups = dict(state['groups']);model.elo.update(state['elo'])
        for team,metrics in state['history'].items():
            for metric,records in metrics.items():model.history[team][metric] = [tuple(r) for r in records]
        for team,records in state['xg'].items():model.xg[team] = [tuple(r) for r in records]
        model.priors.update({tuple(k):list(v) for k,v in state['priors']})
        model.columns['GOALS_FT'] = state['columns'];model.feature_scales['GOALS_FT'] = np.asarray(state['scales'])
        model.fits = state['fits'];model._compile()
        return model


def rolling_predictions(model,history,start,end,metric_values):
    if model.clock > start:raise ValueError('Feature clock would move backwards')
    rows = []
    for day,batch in groupby((r for r in history if start<=r['_date']<end),lambda r:int(r['_date']//86400)):
        batch = list(batch);model.clock = day*86400
        for r in batch:
            bank = model.predict_bank(r['_home'],r['_away'])
            actual = metric_values(r,'GOALS_FT')
            if bank is None or actual is None:continue
            rows.append(dict(fixture=f"{r['_date']}:{r['_home']}:{r['_away']}",date=r['_date'],metric='GOALS_FT',
                regime='XG' if min(len(model.xg[r['_home']]),len(model.xg[r['_away']]))>=8 else 'ELO_NO_XG',
                means=bank[0].tolist(),bootstrapMeans=bank[1:].tolist(),actual=list(actual),
                samples=[len(model.history[r['_home']]['GOALS_FT']),len(model.history[r['_away']]['GOALS_FT'])],
                featureCutoff=model.clock))
        for r in batch:model.observe(r,metric_values)
    model.clock = end
    return rows
