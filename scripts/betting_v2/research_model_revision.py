"""Stage-by-stage model revision on pinned history and cached real prices.

All coefficients and calibrators are fitted before each evaluation month.
Already inspected months remain development evidence. No ROI optimization,
provider/LLM requests, automatic winner selection or production publication.
"""
import argparse
from collections import Counter,defaultdict
import copy
from datetime import datetime,timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import materialize_betting_v2_direct as direct
from betting_v2.reliable_goals import ReliableGoals,CONFIG,rolling_predictions
from betting_v2.selected_joint_calibration import fit_selected,apply_tilt,cell_mass
from betting_v2.training_protocol import fit_joint_profile
from betting_v2.validated_walk_forward import FOLDS,distribution
from betting_v2.research_csv_prices import write,digest,parse_csv,join_rows,recover_schedule_aliases
from betting_v2.research_quotes import assess,select,stats
from betting_v2.joint_count import outcome

ROOT = Path(__file__).resolve().parents[2]
HISTORY_SHA = '1653f6779eece3b7fef7dd5265db1978ffe566ec61e312f5866374739e14e9c0'
ARMS = ('full','current','reliability','recency','calibrated','uncertainty')
LOWER_QUANTILE = .1


def read(path):
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix=='.gz' else raw)


def prediction(row,profile,theta):
    joint = apply_tilt(distribution(row,profile),theta)
    return row,(joint,{s:joint.condition(s) for s in ('HOME','AWAY')}),joint


def prior_price_join(raw_rows,rows,history,aliases,start,end):
    actual = {f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    fixtures=[]
    for r in rows:
        a = actual[r['fixture']]
        code = a['_home_group'];code='ROU' if code=='ROM' else code
        away_code = a['_away_group'];away_code='ROU' if away_code=='ROM' else away_code
        if code != away_code or code not in aliases:continue
        fixtures.append(dict(fixture=r['fixture'],league=code,utcDate=datetime.fromtimestamp(
            r['date'],timezone.utc).date().isoformat(),home=a['_home'],away=a['_away'],
            actual=r['actual'],featureCutoff=r['featureCutoff']))
    doc=dict(testFrom=start,testBefore=end,aliases=aliases,folds=[dict(fixtures=fixtures)])
    doc,audit=recover_schedule_aliases(raw_rows,doc)
    joined,coverage=join_rows(raw_rows,doc)
    return joined,dict(coverage=coverage,aliasEvidence=audit)


def selected_records(rows,joined,profile):
    predictions={r['fixture']:r for r in rows};options=[];bases={}
    for packet in joined:
        row=predictions.get(packet['fixture'])
        if row is None:continue
        qs=[q for q in packet['quotes'] if q['bookmaker']=='Bet365' and q['priceType']=='closing']
        pred=prediction(row,profile,[0.]*4)
        triplet={q['spec'][2]:q['odd'] for q in qs if q['spec'][1]=='RESULT'}
        fav=None
        if set(triplet)=={'HOME','DRAW','AWAY'}:
            s=min(triplet,key=triplet.get)
            if s!='DRAW' and triplet[s]<=1.8:fav=s
        bases[row['fixture']]=cell_mass(pred[2]).tolist()
        for quote in qs:
            q=dict(quote,fixture=row['fixture'],date=row['date'])
            pick=assess(q,pred,profile,fav,'current_preference')
            if pick is not None:
                y,push=outcome(*row['actual'],quote['spec'])
                if push:raise ValueError('Unexpected push in selected calibration')
                options.append(dict(pick,y=y))
    return [dict(fixture=p['fixture'],date=p['date'],spec=p['spec'],y=p['y'],
                 base=bases[p['fixture']]) for p in select(options)]


def calibration_bank(calibration,joined,profile,seed):
    records=selected_records(calibration,joined,profile)
    point=fit_selected(records)
    byfixture={r['fixture']:r for r in calibration};replicas=[]
    weeks=sorted({int(r['date']//604800) for r in records})
    rng=np.random.default_rng(seed)
    for b in range(CONFIG['bootstrapReplicas']):
        boot=[]
        for r in records:
            source=byfixture[r['fixture']]
            row=dict(source,means=source['bootstrapMeans'][b])
            boot.append(dict(r,base=cell_mass(distribution(row,profile)).tolist()))
        weights=dict(zip(weeks,rng.exponential(1.,len(weeks))))
        replicas.append(fit_selected(boot,weights))
    return dict(point=point,replicas=replicas,priorSelectedRecords=records,
        trainingBefore=max((r['date'] for r in records),default=0.),
        labelsUsedForRoiOptimization=False,quantile=LOWER_QUANTILE,
        uncertaintyLimit='Base joint profile hyperparameters and modelling misspecification are not fully propagated')


def lower_probabilities(row,profile,calibration,spec,fav):
    p=[];support=[]
    for means,fit in zip(row['bootstrapMeans'],calibration['replicas']):
        joint=apply_tilt(distribution(dict(row,means=means),profile),fit['theta'])
        p.append(joint.evaluate(spec)[0])
        if fav:
            conditional,_=joint.condition(fav)
            support.append(conditional.evaluate(spec)[0] if conditional else 0.)
    if len(p)!=CONFIG['bootstrapReplicas']:
        raise ValueError('Incomplete uncertainty bank')
    point=apply_tilt(distribution(row,profile),calibration['point']['theta'])
    lower=min(point.evaluate(spec)[0],float(np.quantile(p,LOWER_QUANTILE)))
    support_lower=None
    if support:
        conditional,_=point.condition(fav)
        support_lower=min(conditional.evaluate(spec)[0] if conditional else 0.,float(np.quantile(support,LOWER_QUANTILE)))
    return lower,support_lower


def train_fold(args,history,raw_prices,aliases,fitday,testday,endday,signature):
    out=args.output;stem=out/('fold_'+testday+'.json')
    if stem.exists():
        saved=read(stem)
        if saved['signature']!=signature:raise ValueError('Resume signature mismatch')
        for path,sha in saved['artifactsSha256'].items():
            if digest((out/path).read_bytes())!=sha:raise ValueError('Partial checkpoint changed: '+path)
        print('RESUME_REVISION',testday,flush=True)
        return saved
    started=time.time();base=ReliableGoals()
    design=base.design(history,direct.timestamp(fitday),direct.metric_values)
    print('REVISION_DESIGN',testday,design[0].shape,flush=True)
    metadata={};paths=[]
    for variant,recency,bootstrap in [('reliability',False,False),('recency',True,True)]:
        model=copy.deepcopy(base)
        seed=CONFIG['bootstrapSeed']+int(testday.replace('-',''))
        metadata[variant]=model.fit_bank(design,recency=recency,bootstrap=bootstrap,seed=seed)
        print('REVISION_FITTED',testday,variant,'replicas',len(model.fits)-1,'seconds',round(time.time()-started,1),flush=True)
        calibration=rolling_predictions(model,history,direct.timestamp(fitday),direct.timestamp(testday),direct.metric_values)
        profile=fit_joint_profile(calibration)
        for suffix,value,zipped in [('calibration.json.gz',calibration,True),('pretest_state.json.gz',model.export(),True),('profile.json',profile,False)]:
            path=out/(variant+'_'+testday+'_'+suffix);write(path,value,zipped=zipped);paths.append(path)
        if variant=='recency':
            joined,audit=prior_price_join(raw_prices,calibration,history,aliases,fitday,testday)
            bank=calibration_bank(calibration,joined,profile,seed+1000)
            if bank['trainingBefore']>=direct.timestamp(testday):raise ValueError('Calibration used test labels')
            for suffix,value,zipped in [('selected_calibration.json',bank,False),('prior_price_joins.json.gz',joined,True),('prior_join_audit.json.gz',audit,True)]:
                path=out/(variant+'_'+testday+'_'+suffix);write(path,value,zipped=zipped);paths.append(path)
        later=rolling_predictions(model,history,direct.timestamp(testday),direct.timestamp(endday),direct.metric_values)
        path=out/(variant+'_'+testday+'_predictions.json.gz');write(path,later,zipped=True);paths.append(path)
        print('REVISION_PREDICTED',testday,variant,len(later),'seconds',round(time.time()-started,1),flush=True)
    saved=dict(signature=signature,fitBefore=fitday,testFrom=testday,testBefore=endday,
        metadata=metadata,artifactsSha256={p.name:digest(p.read_bytes()) for p in paths},seconds=round(time.time()-started,2))
    write(stem,saved)
    return saved


def evaluate(args,frozen,joined,model_root=None):
    model_root=model_root or args.output
    options={a:[] for a in ARMS};brier=defaultdict(list);paired=[];coverage=Counter();eligible=set()
    for fold in frozen['folds']:
        day=fold['testFrom'];new={}
        for name in ('reliability','recency'):
            new[name]={r['fixture']:r for r in read(model_root/(name+'_'+day+'_predictions.json.gz'))}
        profiles={name:read(model_root/(name+'_'+day+'_profile.json')) for name in ('reliability','recency')}
        calibrator=read(args.output/('recency_'+day+'_selected_calibration.json'))
        fixtures={f['fixture']:f for f in fold['fixtures']}
        for packet in joined:
            f=fixtures.get(packet['fixture'])
            if f is None:continue
            missing=[name for name in ('reliability','recency') if f['fixture'] not in new[name]]
            for name in missing:coverage[name+'MissingPrediction']+=1
            if missing:continue
            for name in ('reliability','recency'):
                if f['actual']!=new[name][f['fixture']]['actual']:raise ValueError('Paired historical labels differ')
            eligible.add(f['fixture'])
            qs=[q for q in packet['quotes'] if q['bookmaker']=='Bet365' and q['priceType']=='closing']
            triplet={q['spec'][2]:q['odd'] for q in qs if q['spec'][1]=='RESULT'};fav=None
            if set(triplet)=={'HOME','DRAW','AWAY'}:
                side=min(triplet,key=triplet.get)
                if side!='DRAW' and triplet[side]<=1.8:fav=side
            for arm in ARMS:
                if arm in ('full','current'):
                    key='full' if arm=='full' else 'separate_ft_selected_period'
                    row=f['predictions'][key];profile=fold['profiles'][key];theta=[0.]*4
                else:
                    key='reliability' if arm=='reliability' else 'recency';row=new[key][f['fixture']];profile=profiles[key]
                    theta=calibrator['point']['theta'] if arm in ('calibrated','uncertainty') else [0.]*4
                pred=prediction(row,profile,theta)
                for quote in qs:
                    q=dict(quote,fixture=f['fixture'],league=f['league'],date=f['date'],testFrom=day,
                        featureCutoff=f['featureCutoff'],sourceUrl=packet['sourceUrl'],sourceSha256=packet['sourceSha256'],csvRow=packet['csvRow'])
                    y,push=outcome(*f['actual'],q['spec'])
                    probability=pred[2].evaluate(q['spec'])[0]
                    brier[arm,q['spec'][1]].append((probability-y)**2)
                    pick=assess(q,pred,profile,fav,'current_preference')
                    if pick is None:continue
                    if arm=='uncertainty':
                        lower,support_lower=lower_probabilities(row,profile,calibrator,q['spec'],fav)
                        ev_lower=lower*q['odd']-1
                        if lower<.6 or ev_lower<=0 or fav and support_lower<.6:
                            coverage['rejectedByUncertainty']+=1;continue
                        confidence=min(lower,support_lower) if support_lower is not None else lower
                        pick.update(probabilityLower=lower,evLower=ev_lower,scenarioLower=support_lower,confidence=confidence,
                            tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING')
                    options[arm].append(dict(pick,y=y,actualPush=push))
    reports={}
    for arm,candidates in options.items():
        picks=select(candidates)
        reports[arm]=dict(overall=stats(picks),byMonth={f['testFrom']:stats([p for p in picks if p['testFrom']==f['testFrom']]) for f in frozen['folds']},
            byMarket={m:stats([p for p in picks if p['spec'][1]==m]) for m in ('RESULT','TOTAL')},
            byTier={t:stats([p for p in picks if p['tier']==t]) for t in ('HIGH','CONFIRMED','DEVELOPING')},
            pricedBrier={m:dict(events=len(brier[arm,m]),brier=float(np.mean(brier[arm,m]))) for m in ('RESULT','TOTAL') if brier[arm,m]},picks=picks)
        print('REVISION_RESULT',arm,json.dumps(reports[arm]['overall']),flush=True)
    write(args.output/'replay.json.gz',dict(reports=reports,eligibleFixtures=sorted(eligible)),zipped=True)
    return dict(reports={a:{k:v for k,v in r.items() if k!='picks'} for a,r in reports.items()},
        commonPricedFixtures=len(eligible),inputPricedFixtures=len(joined),coverage=dict(coverage))


def main():
    p=argparse.ArgumentParser();p.add_argument('--data-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--prices',type=Path,default=ROOT/'reports/betting_v2/csv_prices_v2_20261016')
    p.add_argument('--csv-cache',type=Path,default=ROOT/'reports/betting_v2/csv_prices_20261016')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    source_files=[Path(__file__),Path(__file__).with_name('reliable_goals.py'),Path(__file__).with_name('selected_joint_calibration.py'),
        Path(__file__).with_name('training_protocol.py'),Path(__file__).with_name('research_quotes.py'),Path(__file__).with_name('joint_count.py'),
        Path(__file__).with_name('validated_walk_forward.py')]
    frozen_bytes=(args.prices/'frozen_predictions.json.gz').read_bytes();manifest=read(args.prices/'input_manifest.json')
    if digest(frozen_bytes)!=manifest['inputSha256']:raise ValueError('Frozen price-prediction input changed')
    frozen=json.loads(gzip.decompress(frozen_bytes));joined=read(args.prices/'joined_quotes.json.gz')
    csv_inventory=read(args.csv_cache/'source_inventory.json');raw_prices=[]
    for source in csv_inventory['sources']:
        raw=gzip.decompress((args.csv_cache/'sources'/source['cachePath']).read_bytes())
        if digest(raw)!=source['sha256']:raise ValueError('Cached CSV bytes changed')
        for r in parse_csv(raw,source):r['sha256']=source['sha256'];raw_prices.append(r)
    direct.ROOT=args.data_root
    history=direct.load_actual_results(direct.timestamp('2026-10-15'))['GLOBAL']
    data_sha=digest(json.dumps(history,sort_keys=True,separators=(',',':')).encode())
    if data_sha!=HISTORY_SHA:raise ValueError('Pinned training history mismatch: '+data_sha)
    signature=digest((data_sha+digest(frozen_bytes)+digest((args.prices/'joined_quotes.json.gz').read_bytes())+
        digest((args.csv_cache/'source_inventory.json').read_bytes())+
        ''.join(digest(p.read_bytes()) for p in source_files)+json.dumps(CONFIG,sort_keys=True)).encode())
    inventory=dict(signature=signature,historySha256=data_sha,historyRows=len(history),config=CONFIG,
        sourceSha256={p.name:digest(p.read_bytes()) for p in source_files},frozenInputSha256=digest(frozen_bytes),
        priceJoinSha256=digest((args.prices/'joined_quotes.json.gz').read_bytes()),folds=FOLDS,
        arms=list(ARMS),untouchedHoldout=False,roiOptimized=False,providerCalls=0,llmCalls=0,publicDownloads=0,
        baseModelScope='FT goal core; goal periods can share this FT joint; other counts remain their frozen components')
    write(args.output/'inventory.json',inventory);write(args.output/'status.json',dict(status='TRAINING',signature=signature))
    for fitday,testday,endday in FOLDS:
        train_fold(args,history,raw_prices,frozen['aliases'],fitday,testday,endday,signature)
        write(args.output/'status.json',dict(status='TRAINING',lastCompletedFold=testday,signature=signature))
    write(args.output/'status.json',dict(status='EVALUATING',signature=signature))
    summary=evaluate(args,frozen,joined)
    summary.update(status='COMPLETE',signature=signature,historySha256=data_sha,config=CONFIG,
        untouchedHoldout=False,roiOptimized=False,providerCalls=0,llmCalls=0,publicDownloads=0,
        inputCommit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        limitations=['All evaluation months were already inspected; this is a development experiment.',
            'Source-labelled closing prices lack exact observation/fill timestamps.',
            'Weekly weight bootstrap and fixed base profile do not cover all model misspecification.',
            'The 10th percentile from 12 replicas is an approximate sensitivity measure, not a certified confidence bound.',
            'Insufficient prior selected samples retain identity calibration, with explicit reason.',
            'No hyperparameter or arm is selected from test ROI; all negative/zero results retained.',
            'FT core changed first; corners/shots/fouls/cards weights are not retrained in this experiment.'])
    write(args.output/'summary.json',summary);write(args.output/'status.json',dict(status='COMPLETE',signature=signature,
        commonPricedFixtures=summary['commonPricedFixtures'],providerCalls=0,llmCalls=0))
    print('REVISION_COMPLETE',summary['commonPricedFixtures'],flush=True)


if __name__=='__main__':main()
