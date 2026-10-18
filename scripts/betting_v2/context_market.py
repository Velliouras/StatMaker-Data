"""Fixed contextual log-odds deviations from complete count-market partitions.

One canonical OVER probability defines both conditional sides. Parameters are
learned with prior fixture-weighted proper loss; no returns or ROI enter a fit.
"""
from collections import Counter
import math
import numpy as np
from scipy.optimize import minimize
from scipy.special import expit,logit

METRICS=('CORNERS','SHOTS','SOT','FOULS')
FEATURES=('bias','stat_market_logit_gap','market_logit','team_scope','home_team',
    'corners','shots','sot','fouls','elo_context','xg_context','goal_form_context',
    'count_recent_residual','count_venue_residual','line_distance','sample_quality',
    'xg_quality','xg_missing','quote_age','hours_to_kickoff','own_margin','independent_reference',
    'delta_sample','delta_xg','delta_elo','delta_form','delta_team',
    'delta_corners','delta_shots','delta_sot','delta_fouls')
SIMPLE=(0,1,2)
MIN_FIXTURES=100
PENALTY=.01
SEED=18102026


def clipped(value,scale=1.):return float(np.clip(value/scale,-1.,1.))


def feature_vector(q,stat_over,market_over,mean,context):
    metric,scope,_,team,line=q['spec']
    if metric not in METRICS or scope not in ('TOTAL','TEAM') or not 0<market_over<1:
        raise ValueError('Unsupported contextual binary market')
    delta=clipped(logit(np.clip(stat_over,1e-12,1-1e-12))-logit(market_over),4.)
    quality=min(context['sample_home'],context['sample_away'])
    quality=quality/(quality+20.)
    xq=min(1.,min(context['xg_home'],context['xg_away'])/8.)
    denominator=math.sqrt(mean+1.)
    recent=clipped(context['count_recent']-mean,4*denominator)
    venue=clipped(context['count_venue']-mean,4*denominator)
    distance=clipped(mean-float(line),4*denominator)
    elo=clipped(context['elo'],3.)
    xg=clipped(context['xg_level']/1.5-1.,2.)
    form=clipped(context['goal_form']/1.5-1.,2.)
    age=max(0.,q['observed']-context['market_updated'])/86400.
    hours=max(0.,q['kickoff']-q['observed'])/3600.
    values=[1.,delta,clipped(logit(market_over),6.),float(scope=='TEAM'),float(team=='HOME'),
        *[float(metric==m) for m in METRICS],elo,xg,form,recent,venue,distance,quality,xq,float(xq<1.),
        min(1.,age),min(1.,math.log1p(hours)/math.log(169.)),clipped(context['margin'],.25),float(context['independent']),
        delta*quality,delta*xq,delta*elo,delta*form,delta*float(scope=='TEAM'),
        *[delta*float(metric==m) for m in METRICS]]
    if len(values)!=len(FEATURES) or not np.isfinite(values).all():raise ValueError('Invalid contextual features')
    return values


def fit(records,cutoff,kind='context',week_weights=None):
    if kind not in ('context','simple'):raise ValueError('Unknown contextual model')
    if any(r['kickoff']+86400>cutoff or r['featureCutoff']>=r['kickoff'] for r in records):
        raise ValueError('Context labels/features cross the prior boundary')
    counts=Counter(r['fixture'] for r in records)
    indexes=list(range(len(FEATURES))) if kind=='context' else list(SIMPLE)
    meta=dict(kind=kind,fixtures=len(counts),events=len(records),trainingBefore=cutoff,labelLagSeconds=86400,
        penalty=PENALTY,objective='Prior fixture-weighted conditional binary log loss with market logit offset',
        roiUsed=False,features=[FEATURES[i] for i in indexes],featureIndexes=indexes)
    if len(counts)<MIN_FIXTURES:return dict(meta,fitted=False,coef=[0.]*len(indexes),reason='INSUFFICIENT_PRIOR_FIXTURES')
    x=np.asarray([r['x'] for r in records])[:,indexes]
    q=np.asarray([r['marketOver'] for r in records]);y=np.asarray([r['yOver'] for r in records])
    if not np.isfinite(x).all() or np.any((q<=0)|(q>=1)) or np.any((y!=0)&(y!=1)):
        raise ValueError('Invalid contextual training input')
    offset=logit(q)
    weights=np.asarray([(week_weights or {}).get(int(r['kickoff']//604800),1.)/counts[r['fixture']] for r in records])
    if not np.isfinite(weights).all() or np.any(weights<=0):raise ValueError('Invalid prior weights')
    weights/=weights.sum()
    def objective(beta):
        z=offset+x@beta;error=weights*(expit(z)-y)
        value=float(weights@(np.logaddexp(0,z)-y*z)+PENALTY*(beta@beta))
        return value,x.T@error+2*PENALTY*beta
    fitted=minimize(objective,np.zeros(len(indexes)),jac=True,method='L-BFGS-B',bounds=[(-2.,2.)]*len(indexes),
        options=dict(maxiter=500,ftol=1e-12,gtol=1e-8))
    if not fitted.success or not np.isfinite(fitted.fun):raise ValueError('Context fit failed '+str(fitted.message))
    return dict(meta,fitted=True,coef=fitted.x.tolist(),loss=float(fitted.fun),maxTrainingKickoff=max(r['kickoff'] for r in records))


def bank(frame,cutoff,kind):
    prior=[r for r in frame if r['kickoff']+86400<=cutoff]
    weeks=sorted({int(r['kickoff']//604800) for r in prior});rng=np.random.default_rng(SEED)
    return dict(point=fit(prior,cutoff,kind),replicas=[fit(prior,cutoff,kind,
        dict(zip(weeks,rng.exponential(size=len(weeks)).tolist()))) for _ in range(12)],
        uncertainty='Weekly parameter sensitivity; prior rate features fixed during training; no calibrated coverage claim')


def predict(x,market_over,parameters):
    if not 0<market_over<1:raise ValueError('Invalid market offset')
    indexes=parameters['featureIndexes'];coef=np.asarray(parameters['coef'])
    v=np.asarray(x)
    if v.shape!=(len(FEATURES),) or not np.isfinite(v).all() or not np.isfinite(coef).all():
        raise ValueError('Invalid contextual prediction')
    return float(expit(logit(market_over)+v[indexes]@coef))
