"""Fit prior GAP statistics and materialize lagged goal-stack training features."""
import argparse,sys
from collections import defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.gap_ratings import METRICS,fit,state,predict
from betting_v2.gap_goal_rates import features
from betting_v2.research_probability_recalibration import ROOT,PRIOR,OCTOBER,FOLDS,MonthlyModels
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.cross_market_inputs import valid_quote
from betting_v2.research_git_archive import absolute_time

def verify(out):
    manifest=read(out/'input_bindings.json');signature=digest((out/'input_bindings.json').read_bytes())
    changed=[p for p,h in manifest['files'].items() if digest((ROOT/p).read_bytes())!=h]
    if changed:raise ValueError('Changed GAP inputs '+str(changed))
    return signature

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();out=args.output;signature=verify(out)
    history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    start=absolute_time(FOLDS[0]+'T00:00:00Z');end=absolute_time('2026-10-15T00:00:00Z')
    targets=defaultdict(dict)
    for fixture,r in truth.items():
        cutoff=int(r['_date']//86400)*86400-86400
        if start<=cutoff and r['_date']<end:
            targets[cutoff][fixture]=dict(home=r['_home'],away=r['_away'],kickoff=r['_date'],statTraining=True,packets=[])
    for packet in ('earliest','latest'):
        quotes=read(PRIOR/f'quotes_{packet}.json.gz')+read(OCTOBER/f'quotes_{packet}.json.gz')
        for q in quotes:
            if valid_quote(q) is not None:continue
            cutoff=q['featureCutoff'];fixture=q['fixture']
            if fixture not in targets[cutoff]:targets[cutoff][fixture]=dict(home=q['_home'],away=q['_away'],kickoff=q['kickoff'],statTraining=False,packets=[])
            item=targets[cutoff][fixture]
            if packet not in item['packets']:item['packets'].append(packet)
    checkpoint=out/'feature_checkpoint.json.gz'
    saved=read(checkpoint) if checkpoint.exists() else dict(signature=signature,done=[],training=[],priced={'earliest':[],'latest':[]},countErrors=[],missingBase=0)
    if saved['signature']!=signature:raise ValueError('GAP feature checkpoint signature mismatch')
    done=set(saved['done']);training=saved['training'];priced=saved['priced'];errors=saved['countErrors'];missing=saved['missingBase']
    # Only CORNERS profile is needed by inherited count setup; rate predictions
    # below call each frozen core directly and never construct count PMFs.
    profiles={d:{'CORNERS':read(PRIOR/f'CORNERS_{d}_coupled_profiles.json')['profiles'] if d!=FOLDS[-1] else read(ROOT/'reports/betting_v2/combined_candidate_20261016/CORNERS_profiles.json')} for d in FOLDS}
    models=MonthlyModels(history,profiles);parameters={};goal_profiles={}
    for cutoff,batch in sorted(targets.items()):
        if cutoff in done:continue
        models.advance(cutoff);fold=models.fold;month=absolute_time(fold+'T00:00:00Z');path=out/f'gap_parameters_{fold}.json'
        if fold not in parameters:
            if path.exists():
                doc=read(path)
                if doc['signature']!=signature:raise ValueError('GAP parameter signature mismatch')
                parameters[fold]=doc['metrics']
            else:
                parameters[fold]={}
                for m in METRICS:
                    parameters[fold][m]=fit(history,m,direct.metric_values,month)
                    print('GAP_FIT',fold,m,{k:v for k,v in parameters[fold][m].items() if k not in ('bounds','features')},flush=True)
                write(path,dict(signature=signature,metrics=parameters[fold]))
        snapshots={m:state(history,m,direct.metric_values,cutoff,parameters[fold][m]) for m in METRICS}
        goal_profiles[fold]=models.goal_profile
        for fixture,target in batch.items():
            h,a=target['home'],target['away'];base=models.ft.predict_bank(h,a)
            if base is None:missing+=1;continue
            gap={m:predict(snapshots[m],h,a) for m in METRICS}
            x=[features(gap,'home'),features(gap,'away')];actual=direct.metric_values(truth[fixture],'GOALS_FT') if fixture in truth else None
            record=dict(fixture=fixture,kickoff=target['kickoff'],featureCutoff=cutoff,fold=fold,home=h,away=a,
                x=x,baseMeanBank=base.tolist(),actual=list(actual) if actual is not None else None,gap=gap,
                regime='XG' if min(len(models.ft.xg[h]),len(models.ft.xg[a]))>=8 else 'ELO_NO_XG')
            if target['statTraining'] and actual is not None:training.append(record)
            for packet in target['packets']:priced[packet].append(record)
            if target['statTraining']:
                for m in METRICS:
                    actual_count=direct.metric_values(truth[fixture],m);baseline=models.counts[m].predict_bank(h,a)
                    if actual_count is not None and baseline is not None:
                        errors.append(dict(fixture=fixture,fold=fold,metric=m,gapMae=float(np.mean(np.abs(np.asarray([gap[m]['home'],gap[m]['away']])-actual_count))),
                            baselineMae=float(np.mean(np.abs(baseline[0]-actual_count))),coldStart=gap[m]['coldStart']))
        done.add(cutoff)
        write(checkpoint,dict(signature=signature,done=sorted(done),training=training,priced=priced,countErrors=errors,missingBase=missing),zipped=True)
        write(out/'progress.json',dict(phase='GAP_FEATURES',completedDays=len(done),totalDays=len(targets),trainingFixtures=len(training),pricedFixtures={k:len(v) for k,v in priced.items()},providerCalls=0,llmCalls=0))
        print('GAP_DAY',cutoff,len(training),{k:len(v) for k,v in priced.items()},flush=True)
    # The profile files are immutable inputs; recover even when all feature days
    # were already checkpointed and the model iterator had nothing left to do.
    for d in FOLDS:
        if d not in goal_profiles:
            models.advance(absolute_time(d+'T00:00:00Z'));goal_profiles[d]=models.goal_profile
    write(out/'goal_profiles.json',goal_profiles)
    write(out/'goal_training_features.json.gz',training,zipped=True)
    for packet,rows in priced.items():write(out/f'goal_features_{packet}.json.gz',rows,zipped=True)
    scores={d:{m:dict(fixtures=sum(r['fold']==d and r['metric']==m for r in errors),
        gapMae=float(np.mean([r['gapMae'] for r in errors if r['fold']==d and r['metric']==m])),
        baselineMae=float(np.mean([r['baselineMae'] for r in errors if r['fold']==d and r['metric']==m]))) for m in METRICS} for d in FOLDS}
    write(out/'feature_summary.json',dict(status='COMPLETE',trainingFixtures=len(training),pricedFixtures={k:len(v) for k,v in priced.items()},countPredictionScores=scores,
        missingBase=missing,providerCalls=0,llmCalls=0,roiUsed=False,signature=signature))

if __name__=='__main__':main()
