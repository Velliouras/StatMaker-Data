"""Expand calibration with all available preceding frozen prediction months.

Reuses the completed 42-fit experiment. No new core fit, thresholds, ROI-based
choice, provider/LLM call, or download. The first narrow-window experiment stays
immutable. Later evaluated months cannot enter earlier calibration pools.
"""
import argparse
from datetime import datetime,timezone
import gzip
import json
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_model_revision import read,prediction,selected_records,prior_price_join,evaluate,ROOT
from betting_v2.research_csv_prices import write,digest,parse_csv,join_rows,recover_schedule_aliases
from betting_v2.selected_joint_calibration import fit_selected,cell_mass
from betting_v2.reliable_goals import CONFIG
from betting_v2.validated_walk_forward import FOLDS,distribution
from betting_v2.opponent_matchup import timestamp


def frame_join(rows,raw_prices,frozen,start,end):
    catalogue={}
    for fold in frozen['folds']:
        for f in fold['fixtures']:
            catalogue[f['fixture']]=f
    fixtures=[]
    for r in rows:
        f=catalogue.get(r['fixture'])
        if f is None:continue
        fixtures.append(dict(f,actual=r['actual'],featureCutoff=r['featureCutoff']))
    doc=dict(testFrom=start,testBefore=end,aliases=frozen['aliases'],folds=[dict(fixtures=fixtures)])
    doc,audit=recover_schedule_aliases(raw_prices,doc);joined,coverage=join_rows(raw_prices,doc)
    return joined,dict(coverage=coverage,aliasEvidence=audit)


def freeze_pool(pool,before,seed):
    if any(r['date']>=before for r in pool):raise ValueError('Calibration pool contains future labels')
    if len({(r['fixture'],tuple(r['spec'])) for r in pool})!=len(pool):
        raise ValueError('Duplicate prior calibration events')
    records=[{k:v for k,v in r.items() if k!='bootstrapBases'} for r in pool]
    point=fit_selected(records);weeks=sorted({int(r['date']//604800) for r in pool})
    rng=np.random.default_rng(seed);replicas=[]
    for b in range(CONFIG['bootstrapReplicas']):
        boot=[dict(r,base=original['bootstrapBases'][b]) for r,original in zip(records,pool)]
        weights=dict(zip(weeks,rng.exponential(1.,len(weeks))))
        replicas.append(fit_selected(boot,weights))
    return dict(point=point,replicas=replicas,priorSelectedRecords=records,
        trainingBefore=max((r['date'] for r in pool),default=0.),
        labelsUsedForRoiOptimization=False,quantile=.1,
        cohort='Expanding prior frozen months; source profiles retained per historical forecast',
        sourceFrames=sorted({r['sourceFrame'] for r in pool}),
        uncertaintyLimit='Fixed base profiles and model misspecification are not fully propagated')


def append_frame(pool,rows,joined,profile,name):
    source={r['fixture']:r for r in rows}
    records=selected_records(rows,joined,profile)
    for r in records:
        row=source[r['fixture']]
        bases=[cell_mass(distribution(dict(row,means=means),profile)).tolist() for means in row['bootstrapMeans']]
        pool.append(dict(r,bootstrapBases=bases,sourceFrame=name))


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    model_root=ROOT/'reports/betting_v2/model_revision_20261016';prices=ROOT/'reports/betting_v2/csv_prices_v2_20261016';cache=ROOT/'reports/betting_v2/csv_prices_20261016'
    frozen=read(prices/'frozen_predictions.json.gz');joined=read(prices/'joined_quotes.json.gz')
    original=read(model_root/'inventory.json');input_hashes={}
    for _,day,_ in FOLDS:
        fold=read(model_root/('fold_'+day+'.json'))
        if fold['signature']!=original['signature']:raise ValueError('Core fold signature mismatch')
        for name,sha in fold['artifactsSha256'].items():
            if digest((model_root/name).read_bytes())!=sha:raise ValueError('Core artifact changed: '+name)
            input_hashes[name]=sha
    raw_prices=[]
    for source in read(cache/'source_inventory.json')['sources']:
        raw=gzip.decompress((cache/'sources'/source['cachePath']).read_bytes())
        if digest(raw)!=source['sha256']:raise ValueError('CSV cache changed')
        for r in parse_csv(raw,source):r['sha256']=source['sha256'];raw_prices.append(r)
    args.output.mkdir(parents=True,exist_ok=True)
    write(args.output/'status.json',dict(status='CALIBRATING',newCoreFits=0,providerCalls=0,llmCalls=0))
    pool=[];frame_audits={}
    for fitday,day,endday in FOLDS:
        profile=read(model_root/('recency_'+day+'_profile.json'))
        if not pool:
            rows=read(model_root/('recency_'+day+'_calibration.json.gz'))
            joined_prior=read(model_root/('recency_'+day+'_prior_price_joins.json.gz'))
            append_frame(pool,rows,joined_prior,profile,'2026-07 prior calibration')
        bank=freeze_pool(pool,timestamp(day),
            CONFIG['bootstrapSeed']+int(day.replace('-',''))+5000)
        write(args.output/('recency_'+day+'_selected_calibration.json'),bank)
        print('EXPANDING_CALIBRATION',day,bank['point']['fixtures'],bank['point']['fitted'],bank['point']['theta'],flush=True)
        # Append this month's original out-of-time predictions only after its
        # calibrator has been frozen. Its labels cannot enter its own fit.
        rows=read(model_root/('recency_'+day+'_predictions.json.gz'))
        joined_frame,audit=frame_join(rows,raw_prices,frozen,day,endday)
        append_frame(pool,rows,joined_frame,profile,day+' out-of-time predictions')
        frame_audits[day]=dict(joinedFixtures=len(joined_frame),selectedPoolSize=len(pool),audit=audit)
    write(args.output/'calibration_frame_audits.json.gz',frame_audits,zipped=True)
    summary=evaluate(args,frozen,joined,model_root=model_root)
    summary.update(status='COMPLETE',newCoreFits=0,coreSignature=original['signature'],
        coreArtifactsSha256=input_hashes,historySha256=original['historySha256'],
        untouchedHoldout=False,roiOptimized=False,providerCalls=0,llmCalls=0,publicDownloads=0,
        protocol='Expanding prior frozen predictions; original core fits and thresholds unchanged',
        limitations=['Previously inspected development months; not an untouched holdout.',
            'July seed uses prior-month calibration predictions/profile; later frames are out-of-time predictions.',
            'Source closing-price timestamps and historical data publication times remain unverified.',
            'Twelve weekly bootstrap replicas give approximate sensitivity, not certified confidence.',
            'Other count-model coefficients remain their own frozen components.'])
    write(args.output/'summary.json',summary);write(args.output/'status.json',dict(status='COMPLETE',
        commonPricedFixtures=summary['commonPricedFixtures'],newCoreFits=0,llmCalls=0,providerCalls=0))


if __name__=='__main__':main()
