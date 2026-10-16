"""Load the revised FT core and separate decision sensitivity for UAT.

The other count components retain their own frozen weights. FT and its periods
use one selected-calibrated joint. No live fit or provider request occurs.
"""
import hashlib
import json
import math
from pathlib import Path
import numpy as np
from .live_factorized import load_factorized,read_bound,FactorizedModel
from .reliable_goals import ReliableGoals
from .opponent_matchup import timestamp
from .exact_forecast_adapter import prepare,forecast

MANIFEST='reports/betting_v2/model_revision_20261016/deployment.json'


class RevisedModel(FactorizedModel):
    def __init__(self,ft,base,profile,calibration,quantile):
        super().__init__(ft,base.period,base.counts,base.offsets)
        self.revision_profile=profile
        self.selected_calibration=calibration
        self.quantile=quantile
        self.uncertainty_cache={}

    def prepared_bank(self,home,away,metric):
        key=home,away,metric
        if key not in self.uncertainty_cache:
            bank=self.ft.predict_bank(home,away);full=self.predict(home,away,'GOALS_FT');period=self.predict(home,away,metric)
            if bank is None or full is None or period is None:return None
            shares=[period[i]/full[i] for i in (0,1)] if metric!='GOALS_FT' else None
            regime='XG' if min(len(self.xg[home]),len(self.xg[away]))>=8 else 'ELO_NO_XG'
            result=[]
            for i,means in enumerate(bank):
                row=dict(metric=metric,regime=regime,means=means.tolist())
                if shares is not None:
                    row.update(fullMeans=means.tolist(),shares=shares,means=[float(means[j]*shares[j]) for j in (0,1)])
                profile=dict(self.revision_profile)
                if i:
                    profile['_selected_joint']=self.selected_calibration['replicas'][i-1]['theta']
                result.append((row,profile,prepare(row,profile)))
            self.uncertainty_cache[key]=result
        return self.uncertainty_cache[key]

    def bounds(self,home,away,spec,odd):
        metric=spec[0]
        if not metric.startswith('GOALS'):return None
        bank=self.prepared_bank(home,away,metric)
        if not bank or len(bank)!=len(self.selected_calibration['replicas'])+1:
            raise ValueError('Missing revised uncertainty bank')
        values=[forecast(row,p,spec,prepared) for row,p,prepared in bank]
        point,replicas=values[0],values[1:]
        def lower(key):return min(float(key(point)),float(np.quantile([key(v) for v in replicas],self.quantile)))
        win=lower(lambda v:v['winProbability']);push=lower(lambda v:v['pushProbability'])
        return dict(win=win,push=push,ev=win*odd+push-1,
            home=lower(lambda v:v['scenarios']['HOME']['winProbability'] or 0.),
            away=lower(lambda v:v['scenarios']['AWAY']['winProbability'] or 0.))


def load_revised(root,history,cutoff,*,include_counts=True):
    base,profile,meta=load_factorized(root,history,cutoff)
    path=Path(root)/MANIFEST
    if not path.exists():return base,profile,meta
    raw=path.read_bytes();manifest=json.loads(raw)
    if manifest['schema']!=1 or manifest['variant']!='reliability_recency_selected_calibration_uncertainty':
        raise ValueError('Unexpected revised deployment')
    if cutoff<timestamp(manifest['calibrationBefore']) or cutoff!=math.floor(cutoff/86400)*86400:
        raise ValueError('Revised feature cutoff precedes calibration or includes a partial day')
    state=read_bound(Path(root),manifest['state']);ft=ReliableGoals.restore(state)
    goal_profile=read_bound(Path(root),manifest['profile']);calibration=read_bound(Path(root),manifest['calibration'])
    if len(ft.fits)!=len(calibration['replicas'])+1 or len(ft.fits)<3:
        raise ValueError('Incomplete revised state/calibration bank')
    if calibration['trainingBefore']>=timestamp(manifest['calibrationBefore']):
        raise ValueError('Selected calibration crosses deployment start')
    for row in sorted(history,key=lambda r:r['_date']):
        if timestamp(manifest['calibrationBefore'])<=row['_date']<cutoff:
            import materialize_betting_v2_direct as direct
            ft.observe(row,direct.metric_values)
    ft.clock=cutoff
    effective={k:v for k,v in profile.items() if not k.startswith('GOALS')}
    effective.update(goal_profile);effective['_selected_joint']=calibration['point']['theta']
    model=RevisedModel(ft,base,effective,calibration,manifest['lowerQuantile'])
    meta.update(variant=manifest['variant'],revisionDeploymentSha256=hashlib.sha256(raw).hexdigest(),
        revisionStateSha256=manifest['state']['sha256'],revisionProfileSha256=manifest['profile']['sha256'],
        selectedCalibrationSha256=manifest['calibration']['sha256'],
        goalFeatures='league-prior smoothed sparse history; adaptive club shrinkage; 180-day training half-life',
        uncertainty='12 UTC-week weighted refits; 10th percentile sensitivity, capped at point prediction',
        uncertaintyScope='FT and derived goal periods only; period coefficients/base profile uncertainty not fully propagated',
        otherCountWeights='unchanged frozen components',decisionProbabilitiesSeparate=True,
        researchDataSha256=manifest['historySha256'],profitabilityValidated=False,empiricalTiersValidated=False)
    if include_counts:
        from .live_counts import attach_counts
        return attach_counts(root,history,cutoff,model,effective,meta)
    return model,effective,meta
