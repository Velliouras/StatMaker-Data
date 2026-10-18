"""Prior-only Poisson rate offsets from forecast GAP statistics, never prices."""
from collections import Counter
import numpy as np
from scipy.optimize import minimize

FEATURES=('bias','log_shots','log_sot','log_corners','sample_quality')
PENALTY=.01

def features(forecasts,side):
    sample=min(min(v['sampleHome'],v['sampleAway']) for v in forecasts.values());quality=min(1.,sample/20.)
    return [1.,*[float(np.clip(np.log(forecasts[m][side]/ref),-1.5,1.5))*quality for m,ref in [('SHOTS',12.),('SOT',4.),('CORNERS',4.5)]],quality]

def fit(records,cutoff,week_weights=None):
    if any(r['kickoff']+86400>cutoff or r['featureCutoff']>=r['kickoff'] for r in records):raise ValueError('GAP goal labels cross prior boundary')
    counts=Counter(r['fixture'] for r in records);meta=dict(fixtures=len(counts),trainingBefore=cutoff,labelLagSeconds=86400,
        features=list(FEATURES),roiUsed=False,penalty=PENALTY,objective='Prior fixture-weighted Poisson count loss with frozen rate offsets')
    if len(counts)<100:return dict(meta,fitted=False,coef=[0.]*len(FEATURES))
    x=np.asarray([r['x'] for r in records]).reshape(-1,len(FEATURES));base=np.asarray([r['baseMeanBank'][0] for r in records]).reshape(-1)
    y=np.asarray([r['actual'] for r in records]).reshape(-1)
    if not np.isfinite(x).all() or not np.isfinite(base).all() or np.any(base<=0) or np.any(y<0) or np.any(y!=np.floor(y)):raise ValueError('Invalid GAP goal design')
    w=np.repeat([(week_weights or {}).get(int(r['kickoff']//604800),1.)/(2*counts[r['fixture']]) for r in records],2)
    if not np.isfinite(w).all() or np.any(w<=0):raise ValueError('Invalid goal fit weights')
    w/=w.sum();offset=np.log(base)
    def objective(beta):
        eta=offset+x@beta;mu=np.exp(eta)
        return float(w@(mu-y*eta)+PENALTY*(beta@beta)),x.T@(w*(mu-y))+2*PENALTY*beta
    result=minimize(objective,np.zeros(len(FEATURES)),jac=True,method='L-BFGS-B',bounds=[(-.7,.7)]+[(-1.,1.)]*(len(FEATURES)-1),options=dict(maxiter=500,ftol=1e-11,gtol=1e-8))
    if not result.success or not np.isfinite(result.fun):raise ValueError('GAP goal rate fit failed '+str(result.message))
    return dict(meta,fitted=True,coef=result.x.tolist(),loss=float(result.fun),maxTrainingKickoff=max(r['kickoff'] for r in records))

def bank(frame,cutoff):
    prior=[r for r in frame if r['kickoff']+86400<=cutoff];weeks=sorted({int(r['kickoff']//604800) for r in prior});rng=np.random.default_rng(18102026)
    return dict(point=fit(prior,cutoff),replicas=[fit(prior,cutoff,dict(zip(weeks,rng.exponential(size=len(weeks)).tolist()))) for _ in range(12)],
        uncertainty='Weekly rate-coefficient and frozen-rate sensitivity, with GAP update parameters fixed; no calibrated coverage claim')

def predict(record,parameters,index=0):
    if not parameters.get('fitted'):raise ValueError('Unfitted GAP goal model')
    means=np.asarray(record['baseMeanBank'][index])*np.exp(np.asarray(record['x'])@np.asarray(parameters['coef']))
    if means.shape!=(2,) or not np.isfinite(means).all() or np.any(means<=0) or np.any(means>12):raise ValueError('GAP goal prediction outside numerical support')
    return means.tolist()
