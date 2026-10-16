"""Frozen revised count banks added to the existing revised UAT goal core."""
import hashlib
import json
from pathlib import Path
import numpy as np
from .live_revised import RevisedModel
from .live_factorized import read_bound
from .reliable_counts import ReliableCounts,CountModels,metric_values
from .opponent_matchup import timestamp
from .exact_forecast_adapter import forecast
from .research_app_markets import prepare_extended as prepare

MANIFEST='reports/betting_v2/all_market_revision_20261016/deployment.json'


class AllMarketModel(RevisedModel):
    def __init__(self,base,counts,profiles):
        super().__init__(base.ft,base,base.revision_profile,base.selected_calibration,base.quantile)
        self.counts=CountModels(counts);self.count_profiles=profiles
        self.history,self.xg=self.counts.history,self.counts.xg

    def prepared_bank(self,home,away,metric):
        if metric not in self.counts.models:return super().prepared_bank(home,away,metric)
        key=home,away,metric
        if key not in self.uncertainty_cache:
            bank=self.counts.predict_bank(home,away,metric)
            if bank is None:return None
            profiles=self.count_profiles[metric]
            if len(bank)!=len(profiles):raise ValueError('Incomplete metric sensitivity profiles')
            regime='XG' if min(len(self.xg[home]),len(self.xg[away]))>=8 else 'ELO_NO_XG'
            values=[]
            for means,p in zip(bank,profiles):
                row=dict(metric=metric,regime=regime,means=means.tolist())
                values.append((row,p,prepare(row,p)))
            self.uncertainty_cache[key]=values
        return self.uncertainty_cache[key]

    def bounds(self,home,away,spec,odd):
        if spec[0] not in self.counts.models:return super().bounds(home,away,spec,odd)
        bank=self.prepared_bank(home,away,spec[0])
        if not bank or len(bank)<3:raise ValueError('Missing count sensitivity bank')
        point,*replicas=[forecast(row,p,spec,prepared) for row,p,prepared in bank]
        def lower(key):return min(float(key(point)),float(np.quantile([key(v) for v in replicas],self.quantile)))
        win=lower(lambda v:v['winProbability']);push=lower(lambda v:v['pushProbability'])
        return dict(win=win,push=push,ev=win*odd+push-1,
            home=lower(lambda v:v['scenarios']['HOME']['winProbability'] or 0.),
            away=lower(lambda v:v['scenarios']['AWAY']['winProbability'] or 0.))


def attach_counts(root,history,cutoff,base,profile,meta,manifest=None):
    root=Path(root);path=root/MANIFEST
    if manifest is None:
        if not path.exists():return base,profile,meta
        raw=path.read_bytes();manifest=json.loads(raw)
    else:raw=json.dumps(manifest,sort_keys=True).encode()
    if manifest['schema']!=1 or timestamp(manifest['calibrationBefore'])>cutoff:
        raise ValueError('Count deployment crosses prediction cutoff')
    models={};profiles={}
    for metric,entry in manifest['metrics'].items():
        model=ReliableCounts.restore(read_bound(root,entry['state']))
        if model.metric!=metric or model.clock!=timestamp(manifest['calibrationBefore']):
            raise ValueError('Unexpected count state metric or feature cutoff')
        profiles[metric]=read_bound(root,entry['profiles'])
        if len(model.fits)!=len(profiles[metric]):raise ValueError('Incomplete count calibration bank')
        for row in history:
            if model.clock<=row['_date']<cutoff:model.observe(row,metric_values)
        model.clock=cutoff;models[metric]=model
    model=AllMarketModel(base,models,profiles)
    effective=dict(profile)
    for bank in profiles.values():effective.update(bank[0])
    model.revision_profile=effective
    metadata=dict(meta,variant='all_market_reliability_recency_uncertainty',
        countRevisionDeploymentSha256=hashlib.sha256(raw).hexdigest(),
        revisedCountMetrics=list(models),otherCountWeights='retrained per metric',
        uncertaintyScope='FT, derived goal periods and all seven count cores; weekly sensitivity is not a calibrated risk guarantee',
        countSelectedPriceCalibration='unavailable before October; no future-priced calibration or ROI tuning',
        cardSettlement='Yellow, yellow+red, and red-count diagnostics remain separate; priced publication requires an explicit contract')
    return model,effective,metadata
