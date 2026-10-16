"""Frozen, separately selected FT/period/count components for the UAT v4 table.

The deployment manifest binds weights, effective profiles and original sample
counts. Frozen pre-October inputs stay inside the states; only completed days
from October onwards are rolled from the current repository. No estimator fits.
"""
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import numpy as np
from betting_v2.live_exact import restore
from betting_v2.opponent_matchup import timestamp
from betting_v2.joint_count import VERSION

MANIFEST = 'reports/betting_v2/uat_factorized_20261016/deployment.json'

def read_bound(root, entry):
    path = (root / entry['path']).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Model artifact outside repository')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry['sha256']:
        raise ValueError('Frozen artifact hash mismatch: '+entry['path'])
    return json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)

class FactorizedModel:
    def __init__(self, ft, period, counts, offsets):
        self.ft, self.period, self.counts = ft, period, counts
        self.offsets = np.asarray(offsets, float)
        if self.offsets.shape != (2,) or not np.isfinite(self.offsets).all():
            raise ValueError('Invalid frozen half offsets')
        self.history, self.xg = counts.history, counts.xg
        self.cache = {}

    def predict(self, home, away, metric):
        key = home, away, metric
        if key not in self.cache:
            if metric == 'GOALS_FT':
                result = self.ft.predict(home, away, metric)
            elif metric in ('GOALS_1H', 'GOALS_2H'):
                full = self.predict(home, away, 'GOALS_FT')
                half = self.period.predict(home, away, metric)
                anchor = self.period.predict(home, away, 'GOALS_FT')
                if full is None or half is None or anchor is None:
                    result = None
                else:
                    shares = np.asarray(half[:2]) / anchor[:2]
                    if metric == 'GOALS_2H': shares = 1-shares
                    if not np.isfinite(shares).all() or np.any(shares<0) or np.any(shares>1):
                        raise ValueError('Invalid predicted half shares')
                    e = np.exp(self.offsets)
                    shares = shares*e/(1-shares+shares*e)
                    if metric == 'GOALS_2H': shares = 1-shares
                    result = (float(full[0]*shares[0]),float(full[1]*shares[1]),*half[2:])
            else:
                result = self.counts.predict(home, away, metric)
            self.cache[key] = result
        return self.cache[key]

def load_factorized(root, history, cutoff):
    import materialize_betting_v2_direct as direct
    root = Path(root)
    raw = (root/MANIFEST).read_bytes()
    manifest = json.loads(raw)
    stateday, fitday = manifest['calibrationBefore'], manifest['fittedBefore']
    if manifest['schema'] != 1 or cutoff < timestamp(stateday):
        raise ValueError('No prior frozen factorized model for publication date')
    if cutoff != math.floor(cutoff/86400)*86400:
        raise ValueError('Feature cutoff must be a completed UTC day')
    models = {}
    for name, entry in manifest['states'].items():
        state = read_bound(root, entry)
        models[name] = restore(state, [], timestamp(fitday), direct.metric_values,
                               sample_counts=entry['sampleCounts'])
    profile = read_bound(root, manifest['profile'])
    model = FactorizedModel(models[manifest['ft']],models[manifest['period']],
                            models[manifest['counts']],manifest['halfOffsets'])
    eligible = sorted((r for r in history if timestamp(stateday)<=r['_date']<cutoff),
                      key=lambda r:r['_date'])
    for row in eligible:
        for component in models.values(): component.observe(row,direct.metric_values)
    return model, profile, dict(model=VERSION, variant=manifest['variant'],
        fittedBefore=fitday,calibrationFrom=fitday,calibrationBefore=stateday,
        featureCutoffUTC=datetime.fromtimestamp(cutoff,timezone.utc).isoformat(),
        deploymentSha256=hashlib.sha256(raw).hexdigest(),
        stateSha256={k:v['sha256'] for k,v in manifest['states'].items()},
        profileSha256=manifest['profile']['sha256'],
        goalSelectionSha256=manifest['goalSelectionSha256'],
        countSelectionSha256=manifest['countSelectionSha256'],
        researchDataSha256=manifest['researchDataSha256'],
        probabilityMethod='exact_joint_pmf',featureRowsRolled=len(eligible),
        preCalibrationInputs='immutable exported states; historical corrections require a new fit',
        publicationTimeVerified=False,empiricalTiersValidated=False,
        profitabilityValidated=False,providerCalls=0)
