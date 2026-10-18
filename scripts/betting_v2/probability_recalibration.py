"""Monotone prior-label calibration of count OVER/UNDER probabilities.

This is a separate decision layer, not a replacement joint count distribution.
One canonical OVER event represents both sides; push mass is preserved. Fits
use every available quoted event, fixture weights, and proper loss, never ROI.
"""
from collections import Counter
import math
import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit

MIN_FIXTURES = 30
PENALTY = .01
BOUNDS = ((-2., 2.), (.25, 2.))
SEED = 18102026


def canonical_event(spec, win, push, y):
    if spec[1] not in ('TOTAL', 'TEAM') or spec[2] not in ('OVER', 'UNDER'):
        return None
    if not all(math.isfinite(v) for v in (win, push, y)) or not 0 <= push < 1:
        raise ValueError('Invalid canonical event')
    p = win / (1 - push)
    if not -1e-10 <= p <= 1 + 1e-10 or y not in (0, 1):
        raise ValueError('Invalid conditional event probability or label')
    return (p, y) if spec[2] == 'OVER' else (1 - p, 1 - y)


def fit(records, cutoff, week_weights=None):
    if any(r['kickoff'] + 86400 > cutoff for r in records):
        raise ValueError('Calibration labels cross the 24-hour lag boundary')
    counts = Counter(r['fixture'] for r in records)
    meta = dict(fixtures=len(counts), events=len(records), trainingBefore=cutoff,
        labelLagSeconds=86400, objective='Prior fixture-weighted binary log loss',
        penalty=PENALTY, roiUsed=False, parameters=['intercept', 'positive_slope'])
    if len(counts) < MIN_FIXTURES:
        return dict(meta, fitted=False, theta=[0., 1.], reason='INSUFFICIENT_PRIOR_FIXTURES')
    p = np.clip([r['pOver'] for r in records], 1e-12, 1-1e-12)
    x = logit(p); y = np.asarray([r['yOver'] for r in records])
    weights = np.asarray([(week_weights or {}).get(int(r['kickoff']//604800), 1.) / counts[r['fixture']] for r in records])
    if not np.isfinite(weights).all() or np.any(weights <= 0):
        raise ValueError('Invalid calibration weights')
    weights /= weights.sum()
    def loss(theta):
        z = theta[0] + theta[1]*x
        difference = expit(z) - y
        prior = np.asarray(theta) - [0., 1.]
        value = float(weights @ (np.logaddexp(0, z) - y*z) + PENALTY*(prior @ prior))
        grad = np.asarray([weights @ difference, weights @ (difference*x)]) + 2*PENALTY*prior
        return value, grad
    result = minimize(loss, [0., 1.], jac=True, method='L-BFGS-B', bounds=BOUNDS,
        options=dict(ftol=1e-12, gtol=1e-8, maxiter=500))
    if not result.success or not np.isfinite(result.fun):
        raise ValueError('Probability calibration failed: '+str(result.message))
    return dict(meta, fitted=True, theta=result.x.tolist(), loss=float(result.fun),
        maxTrainingKickoff=max(r['kickoff'] for r in records))


def bank(frame, metric, scope, cutoff):
    rows = [r for r in frame if r['metric'] == metric and r['scope'] == scope and r['kickoff'] + 86400 <= cutoff]
    weeks = sorted({int(r['kickoff']//604800) for r in rows})
    rng = np.random.default_rng(SEED)
    return dict(point=fit(rows, cutoff), replicas=[fit(rows, cutoff,
        dict(zip(weeks, rng.exponential(size=len(weeks)).tolist()))) for _ in range(12)],
        uncertainty='Weekly parameter sensitivity; not a calibrated confidence guarantee')


def probability(spec, win, push, theta):
    if spec[1] not in ('TOTAL', 'TEAM') or spec[2] not in ('OVER', 'UNDER'):
        raise ValueError('Calibration supports count OVER/UNDER decisions only')
    if len(theta) != 2 or not all(math.isfinite(v) for v in theta) or theta[1] <= 0:
        raise ValueError('Invalid monotone calibration')
    p, _ = canonical_event(spec, win, push, 0.)
    adjusted = float(expit(theta[0] + theta[1]*logit(np.clip(p, 1e-12, 1-1e-12))))
    return (adjusted if spec[2] == 'OVER' else 1-adjusted)*(1-push)


def decision(pick, calibrated_win):
    # A fitted calibration cannot manufacture an upward statistical edge.
    p = min(pick['p'], calibrated_win)
    ev = p*pick['odd'] + pick['push'] - 1
    if not 1.8 < pick['odd'] < 3 or p < .6 or ev <= 0:
        return None
    confidence = min(pick['confidence'], p)
    return dict(pick, p=p, ev=ev, confidence=confidence,
        tier='HIGH' if confidence >= .8 else 'CONFIRMED' if confidence >= .7 else 'DEVELOPING')
