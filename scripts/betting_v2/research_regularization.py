"""Offline ridge study and chronological market-family model selection.

The earlier 2/3 of the calibration month fits validation profiles; the later
1/3 selects a candidate and calibration strength. After selection, the profile
is refitted on the entire calibration month and frozen for the next test month.
All three goal periods share one choice to preserve full/half coherence.
Previously inspected test periods are development evidence, not a holdout.
"""
import argparse
from collections import defaultdict
import gzip,hashlib,json,math,sys,time
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.opponent_matchup import timestamp
from betting_v2.chronological_stack import ChronologicalStack,PriorGoalBank
from betting_v2.validated_walk_forward import FOLDS,rolling_rows,event_rows,export_state,zipped
from betting_v2.training_protocol import fit_joint_profile
from betting_v2.recovery_walk_forward import write,summary
from betting_v2.research_checkpoint import persist

RIDGES=(10.,50.)
MODES=('joint','half_shrink','identity')
BASES=('full','no_xg','no_goal_stack','oof_goal_stack')
GROUPS={'GOALS':('GOALS_FT','GOALS_1H','GOALS_2H'),
        **{m:(m,) for m in ('CORNERS','SHOTS','SOT','YELLOW_CARDS','CARDS','FOULS')}}

def read_gz(path):return json.loads(gzip.decompress(path.read_bytes()))
def row_key(r):return r['fixture'],r['metric']
def event_key(r):return r['fixture'],r['metric'],r['market']
def candidate_names():return (*BASES,*(f'oof_ridge_{int(v)}' for v in RIDGES))

def profile_mode(profile,mode):
    if mode=='identity':return {}
    if mode=='joint':return profile
    if mode!='half_shrink':raise ValueError('Unknown calibration mode')
    # Shrink the joint parameters, rather than independently rescaling market
    # probabilities: complements, pushes and goal-period thinning stay coherent.
    return {key:dict(values,scale=math.sqrt(values.get('scale',1.)),
        alpha=.5*values.get('alpha',0.),draw_tilt=.5*values.get('draw_tilt',0.),
        btts_tilt=.5*values.get('btts_tilt',0.)) for key,values in profile.items()}

def split_calibration(rows,start,end):
    boundary=start+int(((end-start)/86400)*2/3)*86400
    earlier=[r for r in rows if start<=r['date']<boundary]
    later=[r for r in rows if boundary<=r['date']<end]
    if not earlier or not later:raise ValueError('Empty chronological selection window')
    return boundary,earlier,later

def common_rows(candidates):
    common=set.intersection(*(set(map(row_key,v)) for v in candidates.values()))
    return {name:[r for r in rows if row_key(r) in common] for name,rows in candidates.items()}

def select_choices(options):
    """Only validation events enter here; common coverage across all options."""
    common=set.intersection(*(set(map(event_key,rows)) for rows in options.values()))
    diagnostics={};by_group={g:[] for g in GROUPS}
    for (candidate,mode),rows in options.items():
        by_metric=defaultdict(list)
        for r in rows:
            if event_key(r) in common:by_metric[r['metric']].append(r)
        scores={m:float(np.mean([(r['q']-r['y'])**2 for r in sample])) for m,sample in by_metric.items()}
        diagnostics[candidate+'::'+mode]=dict(brierByMetric=scores,events=sum(map(len,by_metric.values())),
            fixtures=len({r['fixture'] for v in by_metric.values() for r in v}))
        for group,metrics in GROUPS.items():
            if not all(m in scores for m in metrics):raise ValueError('Missing selection metric '+group)
            value=float(np.mean([scores[m] for m in metrics]))
            by_group[group].append((value,candidate,mode))
    # Insertion order breaks exact ties; no test-dependent tolerance or override.
    choices={g:dict(candidate=(v:=min(values,key=lambda x:x[0]))[1],mode=v[2],
        validationBrier=v[0]) for g,values in by_group.items()}
    return choices,diagnostics,len(common)

def train(args,history,signature):
    out=args.output;bank=PriorGoalBank(history,direct.metric_values)
    bank.cache={float(k):v for k,v in read_gz(out/'prior_goal_bank.json.gz').items()}
    for factor in RIDGES:
        name=f'oof_ridge_{int(factor)}'
        for i,(fitday,testday,endday) in enumerate(FOLDS):
            stem=name+'_'+testday;checkpoint=out/(stem+'.json.gz')
            if checkpoint.exists():
                if read_gz(checkpoint)['signature']!=signature:raise ValueError('Experiment checkpoint mismatch')
                print('RESUME',stem,flush=True);continue
            started=time.time();print('FIT',stem,flush=True)
            anchor=read_gz(args.baseline/('full_'+testday+'_pretest_state.json.gz'))['weights']['GOALS_FT']
            model=ChronologicalStack(history,timestamp(fitday),direct.metric_values,
                bank=bank,goal_anchor=anchor,ridge_factor=factor)
            calibration=rolling_rows(model,history,timestamp(fitday),timestamp(testday))
            profile=fit_joint_profile(calibration)
            zipped(out/(stem+'_pretest_state.json.gz'),export_state(model))
            zipped(out/(stem+'_calibration.json.gz'),calibration);write(out/(stem+'_parameters.json'),profile)
            print('CALIBRATED',stem,len(calibration),round(time.time()-started,1),flush=True)
            later=rolling_rows(model,history,timestamp(testday),timestamp(endday))
            zipped(out/(stem+'_predictions.json.gz'),later)
            events=event_rows(later,profile)
            base_goals={event_key(r):r['p'] for r in read_gz(args.baseline/('full_'+testday+'.json.gz'))['events']
                if r['metric']=='GOALS_FT'}
            goal_delta=max(abs(r['p']-base_goals[event_key(r)]) for r in events if r['metric']=='GOALS_FT')
            if goal_delta>1e-12:raise ValueError('Frozen goal anchor changed')
            zipped(checkpoint,dict(signature=signature,variant=name,ridgeFactor=factor,
                trainBefore=fitday,testFrom=testday,testBefore=endday,goalBaseMaxDifference=goal_delta,
                events=events,seconds=time.time()-started))
            print('CHECKPOINT',stem,len(events),round(time.time()-started,1),flush=True)
            persist(out,f'Ridge research: {name}, fold {i+1}/3 completed; model selection pending')

def location(args,name):return args.baseline if name in BASES[:3] else args.output

def select(args,signature):
    out=args.output;folds=[]
    for fitday,testday,endday in FOLDS:
        options={};coverage={};boundary=None
        for name in candidate_names():
            rows=read_gz(location(args,name)/(name+'_'+testday+'_calibration.json.gz'))
            boundary,earlier,later=split_calibration(rows,timestamp(fitday),timestamp(testday))
            profile=fit_joint_profile(earlier)
            coverage[name]=dict(profileRows=len(earlier),validationRows=len(later),
                maxProfileTimestamp=max(r['date'] for r in earlier),maxSelectionTimestamp=max(r['date'] for r in later))
            for mode in MODES:options[name,mode]=event_rows(later,profile_mode(profile,mode))
            print('VALIDATED',testday,name,flush=True)
        choices,diagnostics,common=select_choices(options)
        folds.append(dict(testFrom=testday,testBefore=endday,profileFrom=fitday,
            selectionFromUTC=boundary,selectionBefore=testday,choices=choices,
            commonValidationEvents=common,coverage=coverage,validationScores=diagnostics))
        write(out/'regularization_choices.json',dict(signature=signature,folds=folds,
            status='COMPLETE' if len(folds)==len(FOLDS) else 'PARTIAL',
            rule='Minimum mean of per-metric validation Brier; coherent goal-period group; common event cohort',
            noTestRowsRead=True,previouslyInspectedDevelopmentPeriods=True))
        print('SELECTED',testday,json.dumps(choices),flush=True)
        persist(out,f'Prior-window model choices frozen for {len(folds)}/3 folds; evaluation pending')

def evaluate(args,signature):
    out=args.output;selection=json.loads((out/'regularization_choices.json').read_text())
    if selection['signature']!=signature or selection['status']!='COMPLETE':raise ValueError('Choices not frozen')
    all_paired=[];fold_reports=[];ridge_paired=defaultdict(list);selected_events=[]
    for fold in selection['folds']:
        day=fold['testFrom'];baseline=read_gz(args.baseline/('full_'+day+'.json.gz'))['events']
        base_index={event_key(r):r for r in baseline};needed={(v['candidate'],v['mode']) for v in fold['choices'].values()}
        raw_predictions={name:read_gz(location(args,name)/(name+'_'+day+'_predictions.json.gz')) for name in candidate_names()}
        common=common_rows(raw_predictions);cohort=set(map(row_key,next(iter(common.values()))))
        options={}
        for name,mode in needed:
            profile=json.loads((location(args,name)/(name+'_'+day+'_parameters.json')).read_text())
            options[name,mode]=event_rows(common[name],profile_mode(profile,mode))
        chosen=[]
        for group,choice in fold['choices'].items():
            for r in options[choice['candidate'],choice['mode']]:
                if r['metric'] in GROUPS[group]:chosen.append(dict(r,selectedCandidate=choice['candidate'],calibrationMode=choice['mode']))
        paired=[]
        for r in chosen:
            b=base_index.get(event_key(r))
            if b is None or b['y']!=r['y']:raise ValueError('Paired evaluation mismatch')
            paired.append(dict(r,p=b['q']))
        all_paired.extend(paired);selected_events.extend(chosen)
        fold_reports.append(dict(testFrom=day,choices=fold['choices'],paired=family_summary(paired),
            candidatePredictionRows={k:len(v) for k,v in raw_predictions.items()},commonPredictionRows=len(cohort)))
        for name in candidate_names()[4:]:
            for r in read_gz(out/(name+'_'+day+'.json.gz'))['events']:
                b=base_index.get(event_key(r))
                if b is not None and row_key(r) in cohort:ridge_paired[name].append(dict(r,p=b['q']))
        print('EVALUATED',day,len(paired),flush=True)
    result=dict(signature=signature,folds=fold_reports,selectedVsFull=family_summary(all_paired),
        ridgeVsFull={n:family_summary(r) for n,r in ridge_paired.items()},
        probabilityBands=probability_bands(all_paired),commonEvents=len(all_paired),
        untouchedHoldout=False,pricedBets=0,providerCalls=0,productionEnabled=False,
        liveChanges=0,intervals='Paired fixture-bootstrap; marginal, no multiplicity adjustment',
        limitations=['Previously inspected development months','Unpriced fixed event grid, not selected betting tiers',
            'Historical statistics publication timestamps not verified','Model/calibration menu designed after prior development inspection'])
    write(out/'regularization_comparison.json',result)
    zipped(out/'regularization_selected_events.json.gz',selected_events)
    print('COMPLETE',json.dumps({m:round(s['candidate']['brier']-s['baseline']['brier'],7)
        for m,s in result['selectedVsFull'].items()}),flush=True)
    persist(out,'Completed ridge and chronological market-family selection study; live model unchanged')

def family_summary(rows):
    grouped=defaultdict(list)
    for r in rows:grouped[r['metric']].append(r)
    return {m:summary(v) for m,v in grouped.items()}

def probability_bands(rows):
    result={}
    for metric in sorted({r['metric'] for r in rows}):
        result[metric]={}
        for lo,hi,label in ((.6,.7,'60-70'),(.7,.8,'70-80'),(.8,1.000001,'80-100')):
            sample=[r for r in rows if r['metric']==metric and lo<=r['q']<hi]
            if not sample:continue
            groups=defaultdict(list)
            for r in sample:groups[r['fixture']].append(r['y']-r['q'])
            sums=np.asarray([sum(v) for v in groups.values()]);counts=np.asarray([len(v) for v in groups.values()])
            rng=np.random.default_rng(16102026);boot=[]
            for _ in range(1000):
                ix=rng.integers(len(sums),size=len(sums));boot.append(sums[ix].sum()/counts[ix].sum())
            result[metric][label]=dict(events=len(sample),fixtures=len(groups),
                predicted=float(np.mean([r['q'] for r in sample])),observed=float(np.mean([r['y'] for r in sample])),
                observedMinusPredicted95=list(map(float,np.quantile(boot,[.025,.975]))))
    return result

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True);ap.add_argument('--stage',choices=('train','select','evaluate','all'),default='all')
    args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    history=direct.load_actual_results(timestamp('2026-10-15'))['GLOBAL']
    data_sha=hashlib.sha256(json.dumps(history,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if data_sha!=json.loads((args.baseline/'inventory.json').read_text())['dataSha256']:raise ValueError('Different baseline data')
    source_sha=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('chronological_stack.py').read_bytes()).hexdigest()
    signature=hashlib.sha256((data_sha+source_sha).encode()).hexdigest()
    write(args.output/'regularization_inventory.json',dict(signature=signature,dataSha256=data_sha,sourceSha256=source_sha,
        ridgeFactors=RIDGES,candidates=candidate_names(),calibrationModes=MODES,folds=FOLDS,
        goalBase='Saved baseline weights held identical',halfSplitRidge='Logistic C=0.5/ridgeFactor',
        selectionProtocol='First 2/3 calibration month fits profile; last 1/3 selects; full calibration month refits final profile',
        providerCalls=0,untouchedHoldout=False,productionEnabled=False))
    if args.stage in ('train','all'):train(args,history,signature)
    if args.stage in ('select','all'):select(args,signature)
    if args.stage in ('evaluate','all'):evaluate(args,signature)

if __name__=='__main__':main()
