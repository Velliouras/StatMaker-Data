"""Count GLM with explicit increasing strength/attack semantics."""
import numpy as np
from scipy.optimize import minimize
from sklearn.linear_model import PoissonRegressor

ATTACK_METRICS = {'GOALS_FT','GOALS_1H','GOALS_2H','SHOTS','SOT','CORNERS'}
INCREASING = {'elo_gap','goal_strength_gap','recent_for','recent_opponent_against',
    'venue_for','venue_opponent_against','xg_for','opponent_xg_against'}

def fit_monotone_count(x,y,columns,metric,alpha):
    fit=PoissonRegressor(alpha=alpha,solver='newton-cholesky',max_iter=80,tol=1e-6).fit(x,y)
    constrained=[i for k,i in columns.items() if metric in ATTACK_METRICS and k in INCREASING]
    if not constrained:
        return fit
    initial=np.r_[fit.intercept_,fit.coef_]
    for i in constrained:
        initial[i+1]=max(0,initial[i+1])
    bounds=[(None,None)]+[(0,None) if i in constrained else (None,None) for i in range(x.shape[1])]
    n=float(len(y))
    def objective(theta):
        intercept,beta=theta[0],theta[1:]
        eta=np.asarray(x@beta).ravel()+intercept
        # Numerical line-search protection; no artificial rate clipping in predictions.
        if not np.isfinite(eta).all() or eta.max()>600:
            return np.inf,np.zeros_like(theta)
        mu=np.exp(eta)
        residual=(mu-y)/n
        value=float(np.mean(mu-y*eta)+alpha*.5*(beta@beta))
        gradient=np.r_[float(residual.sum()),np.asarray(x.T@residual).ravel()+alpha*beta]
        return value,gradient
    solved=minimize(objective,initial,jac=True,bounds=bounds,method='L-BFGS-B',
        options=dict(maxiter=4000,ftol=1e-11,gtol=1e-6,maxls=50))
    if not solved.success or not np.isfinite(solved.x).all():
        raise ValueError('Monotone count fit failed: '+str(solved.message))
    fit.intercept_=float(solved.x[0]);fit.coef_=solved.x[1:]
    if any(fit.coef_[i]<-1e-12 for i in constrained):
        raise ValueError('Inverted strength semantics in fitted count model')
    return fit
