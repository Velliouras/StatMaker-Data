"""Prior-window calibration on selected prices, preserving one FT joint PMF.

Fit four regularized tilts on six RESULT x total-2.5 cells using Brier loss.
Price/ROI never enter the objective. Selection is fixed before fitting. Weekly
bootstrap fits quantify calibration sensitivity; no guaranteed lower bound.
"""
import numpy as np
from scipy.optimize import minimize
from .joint_count import JointCount

PENALTY = 10.
MIN_FIXTURES = 30
BOUNDS = [(-.75,.5),(-1.,1.),(-.75,.5),(-1.,1.)]


def cell_indices(shape):
    h,a = np.indices(shape)
    result = np.where(h>a,0,np.where(h==a,1,2))
    total = (h+a>2).astype(int)
    return result*2+total


def cell_mass(joint):
    idx = cell_indices(joint.pmf.shape)
    return np.asarray([joint.pmf[idx==i].sum() for i in range(6)])


def design(base):
    base = np.asarray(base,float)
    result = base.reshape(-1,3,2).sum(axis=2)
    total = base.reshape(-1,3,2).sum(axis=1)
    result_ids = np.repeat(np.arange(3),2);total_ids = np.tile(np.arange(2),3)
    return np.stack((np.log(np.maximum(result[:,result_ids],1e-12)),
        np.broadcast_to((result_ids==1).astype(float),(len(base),6)),
        np.log(np.maximum(total[:,total_ids],1e-12)),
        np.broadcast_to(total_ids.astype(float),(len(base),6))),axis=2)


def event_mask(spec):
    _,scope,side,_,line = spec
    if scope == 'RESULT':
        return np.repeat((np.arange(3)=={'HOME':0,'DRAW':1,'AWAY':2}[side]).astype(float),2)
    if scope == 'TOTAL' and line == 2.5:
        return np.tile((np.arange(2)==int(side=='OVER')).astype(float),3)
    raise ValueError('Selected calibration accepts FT RESULT or total 2.5 only')


def fit_selected(records,week_weights=None):
    count = len({r['fixture'] for r in records})
    empty = dict(theta=[0.,0.,0.,0.],fitted=False,fixtures=count,events=len(records),
                 penalty=PENALTY,objective='fixture-weighted Brier on fixed prior selected cohort')
    if count < MIN_FIXTURES:return dict(empty,reason='INSUFFICIENT_PRIOR_SELECTED_FIXTURES')
    base = np.asarray([r['base'] for r in records]);x = design(base)
    masks = np.asarray([event_mask(r['spec']) for r in records]);y = np.asarray([r['y'] for r in records])
    counts = {}
    for r in records:counts[r['fixture']] = counts.get(r['fixture'],0)+1
    weights = np.asarray([(week_weights or {}).get(int(r['date']//604800),1.)/counts[r['fixture']] for r in records])
    if not np.isfinite(weights).all() or np.any(weights<=0):raise ValueError('Invalid calibration weights')
    logbase = np.log(np.maximum(base,1e-15))
    def objective(theta):
        z = logbase+x@theta;z -= z.max(axis=1,keepdims=True)
        p6 = np.exp(z);p6 /= p6.sum(axis=1,keepdims=True)
        p = (p6*masks).sum(axis=1)
        gradient = (p6[:,:,None]*masks[:,:,None]*x).sum(axis=1)-p[:,None]*(p6[:,:,None]*x).sum(axis=1)
        return float(weights@((p-y)**2)+PENALTY*(theta@theta)), 2*((weights*(p-y))[:,None]*gradient).sum(axis=0)+2*PENALTY*theta
    result = minimize(objective,np.zeros(4),jac=True,bounds=BOUNDS,method='L-BFGS-B',options=dict(ftol=1e-12,gtol=1e-7,maxiter=500))
    if not result.success:raise ValueError('Selected joint calibration failed: '+str(result.message))
    return dict(empty,theta=result.x.tolist(),fitted=True,reason='FITTED_PRIOR_WINDOW_ONLY',loss=float(result.fun))


def apply_tilt(joint,theta):
    theta = np.asarray(theta,float)
    if theta.shape != (4,) or not np.isfinite(theta).all():raise ValueError('Invalid selected joint parameters')
    if not theta.any():return joint
    weights = design(cell_mass(joint)[None,:])[0]@theta
    idx = cell_indices(joint.pmf.shape)
    adjusted = joint.pmf*np.exp(weights[idx]-weights.max())
    bound = min(1.,joint.omitted_mass*float(np.exp(weights.max()-weights.min())))
    return JointCount(adjusted,bound)
