"""Offline corrected-data study. Atomic resumable checkpoints, no publication.

Rolling state, frozen coefficients; not daily refits. Previously inspected test
periods remain development evidence. No historical publication-time guarantee.
"""
import argparse
from collections import defaultdict
import gzip,hashlib,json,os,sys,time
from itertools import groupby
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.opponent_matchup import OpponentMatchup,timestamp
from betting_v2.joint_count import JointCount,outcome
from betting_v2.training_protocol import fit_joint_profile
from betting_v2.recovery_walk_forward import write,LINES

METRICS=tuple(direct.METRIC_COLUMNS)
FOLDS=[('2026-07-01','2026-08-01','2026-09-01'),('2026-08-01','2026-09-01','2026-10-01'),('2026-09-01','2026-10-01','2026-10-15')]
VARIANTS={'full':{},'no_xg':{'feature_exclusions':('xg',)},'no_goal_stack':{'use_goal_strength':False}}

def zipped(path,value):
    tmp=path.with_suffix('.tmp')
    tmp.write_bytes(gzip.compress(json.dumps(value,allow_nan=False,separators=(',',':')).encode(),mtime=0));tmp.replace(path)

def rolling_rows(model,history,start,end):
    rows=[]
    for day,batch in groupby((r for r in history if start<=r['_date']<end),lambda r:int(r['_date']//86400)):
        batch=list(batch)
        for r in batch:
            h,a=r['_home'],r['_away']
            regime='XG' if len(model.xg[h])>=8 and len(model.xg[a])>=8 else 'ELO_NO_XG'
            full=model.predict(h,a,'GOALS_FT')
            for metric in METRICS:
                actual=direct.metric_values(r,metric)
                if actual is None:continue
                pred=full if metric=='GOALS_FT' else model.predict(h,a,metric)
                if pred is None:continue
                item=dict(fixture=f"{r['_date']}:{h}:{a}",date=r['_date'],metric=metric,regime=regime,
                          means=list(pred[:2]),actual=actual,samples=list(pred[2:4]))
                if metric in ('GOALS_1H','GOALS_2H'):
                    if full is None:continue
                    item.update(fullMeans=list(full[:2]),shares=[pred[i]/full[i] for i in (0,1)])
                rows.append(item)
        for r in batch:model.observe(r,direct.metric_values)
    return rows

def grid(metric):
    specs=[]
    for line in LINES.get(metric,(.5,1.5,2.5)):
        for side in ('OVER','UNDER'):specs.append((metric,'TOTAL',side,'',line))
        for team in ('HOME','AWAY'):
            for side in ('OVER','UNDER'):specs.append((metric,'TEAM',side,team,int(line/2)+.5))
    if metric in ('GOALS_FT','GOALS_1H','GOALS_2H','CORNERS','SHOTS','SOT'):
        specs.extend((metric,'RESULT',side,'',None) for side in ('HOME','DRAW','AWAY'))
    if metric.startswith('GOALS'):
        specs.extend((metric,'DC',side,'',None) for side in ('HOME_OR_DRAW','AWAY_OR_DRAW','HOME_OR_AWAY'))
        specs.extend((metric,'BTTS',side,'',None) for side in ('YES','NO'))
        specs.extend((metric,'DNB',side,'',None) for side in ('HOME','AWAY'))
    return list(dict.fromkeys(specs))

def distribution(row,profile):
    metric=row['metric'];key=('GOALS_FT' if metric.startswith('GOALS') else metric)+'::'+row['regime']
    params={k:v for k,v in profile.get(key,{}).items() if k in ('scale','alpha','draw_tilt','btts_tilt')}
    j=JointCount.from_means(*row.get('fullMeans',row['means']),**params)
    if metric.startswith('GOALS') and profile.get('_selected_joint'):
        from .selected_joint_calibration import apply_tilt
        j=apply_tilt(j,profile['_selected_joint'])
    return j.thin(*row['shares']) if 'shares' in row else j

def event_rows(rows,profile):
    events=[]
    for r in rows:
        raw=distribution(r,{});cal=distribution(r,profile)
        for spec in grid(r['metric']):
            p,push=raw.evaluate(spec);q,qpush=cal.evaluate(spec);y,ypush=outcome(*r['actual'],spec)
            # Conditional DNB scoring excludes actual pushes; raw checkpoints
            # retain all counts for three-way settlement/EV replay.
            if ypush:continue
            events.append(dict(fixture=r['fixture'],metric=r['metric'],regime=r['regime'],
                market='|'.join(map(str,spec[1:])),p=p/(1-push),q=q/(1-qpush),y=y,
                push=push,candidatePush=qpush))
    return events

def export_state(model):
    weights={m:dict(intercept=float(f.intercept_),coef=f.coef_.tolist(),columns=model.columns[m],
        scales=model.feature_scales[m].tolist()) for m,f in model.models.items()}
    if model.period_model is not None:
        weights['period']=dict(intercept=model.period_model.intercept_.tolist(),coef=model.period_model.coef_.tolist(),
                              columns=model.period_columns,scales=model.period_scales.tolist())
    return dict(weights=weights,history={k:dict(v) for k,v in model.history.items()},elo=dict(model.elo),
                xg=dict(model.xg),groups=model.groups,training=model.matches)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--variants',nargs='+',choices=list(VARIANTS),default=list(VARIANTS))
    args=ap.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    lock=out/'running.lock';fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    os.write(fd,str(os.getpid()).encode());os.close(fd)
    try:
        history=direct.load_actual_results(timestamp('2026-10-15T00:00:00Z'))['GLOBAL']
        data_hash=hashlib.sha256(json.dumps(history,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        sources=[Path(__file__),Path(direct.__file__)]+[Path(__file__).with_name(n+'.py') for n in
            ('opponent_matchup','monotone_count','joint_count','training_protocol','recovery_walk_forward')]
        source_hash=hashlib.sha256(b''.join(p.read_bytes() for p in sources)).hexdigest()
        signature=hashlib.sha256((data_hash+source_hash).encode()).hexdigest()
        manifest=dict(dataSha256=data_hash,sourceSha256=source_hash,signature=signature,pid=os.getpid(),matches=len(history),
            metrics={m:sum(direct.metric_values(r,m) is not None for r in history) for m in METRICS},
            providerCalls=0,publicationTimeVerified=False,untouchedHoldout=False,folds=FOLDS,variants=args.variants,
            utcDayBatchedUpdates=True,productionEnabled=False)
        write(out/'inventory.json',manifest);print('START',json.dumps(manifest),flush=True)
        for variant in args.variants:
            for fitday,testday,endday in FOLDS:
                stem=variant+'_'+testday;checkpoint=out/(stem+'.json.gz')
                if checkpoint.exists():
                    saved=json.loads(gzip.decompress(checkpoint.read_bytes()))
                    if saved['signature']!=signature:raise ValueError('Checkpoint fingerprint mismatch')
                    print('RESUME',stem,flush=True);continue
                start=time.time();print('FIT',stem,flush=True)
                model=OpponentMatchup(history,timestamp(fitday),direct.metric_values,**VARIANTS[variant])
                calibration=rolling_rows(model,history,timestamp(fitday),timestamp(testday))
                profile=fit_joint_profile(calibration)
                zipped(out/(stem+'_pretest_state.json.gz'),export_state(model))
                zipped(out/(stem+'_calibration.json.gz'),calibration);write(out/(stem+'_parameters.json'),profile)
                print('CALIBRATED',stem,len(calibration),'seconds',round(time.time()-start,1),flush=True)
                later=rolling_rows(model,history,timestamp(testday),timestamp(endday))
                zipped(out/(stem+'_predictions.json.gz'),later)
                events=event_rows(later,profile)
                zipped(checkpoint,dict(signature=signature,variant=variant,trainBefore=fitday,testFrom=testday,
                    testBefore=endday,training=model.matches,events=events,seconds=round(time.time()-start,2)))
                print('CHECKPOINT',stem,'events',len(events),'seconds',round(time.time()-start,1),flush=True)
        print('COMPLETE',out,flush=True)
    finally:lock.unlink(missing_ok=True)

if __name__=='__main__':main()
