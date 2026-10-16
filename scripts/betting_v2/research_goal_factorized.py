"""Separate full-goal calibration from the conditional first/second-half split.

Primary control locks the existing full-time distribution. An experimental arm
selects FT calibration using FT events only. Both select the half split using
prior-window conditional binomial log loss, never future FT labels. All period
forecasts thin the same FT joint PMF. These inspected months are development.
"""
import argparse,gzip,hashlib,json,sys
from collections import defaultdict
from pathlib import Path
import numpy as np
from scipy.optimize import minimize
from scipy.special import expit,logit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_regularization import (BASES,MODES,candidate_names,location,
    read_gz,profile_mode,split_calibration,event_key)
from betting_v2.research_event_grid import validation_events
from betting_v2.validated_walk_forward import FOLDS,zipped,distribution
from betting_v2.training_protocol import fit_joint_profile
from betting_v2.opponent_matchup import timestamp
from betting_v2.recovery_walk_forward import summary,write
from betting_v2.research_checkpoint import persist

GOALS=('GOALS_FT','GOALS_1H','GOALS_2H')
ARMS=('locked_ft_selected_period','separate_ft_selected_period')
FT_BASES=('full','no_xg','goal_ridge_10','goal_ridge_50')
OFFSET_PENALTY=5.
OFFSET_MIN_FIXTURES=100

def records(ft_rows,half_rows):
    full={r['fixture']:r for r in ft_rows if r['metric']=='GOALS_FT'};result=[]
    for r in half_rows:
        if r['metric']!='GOALS_1H' or r['fixture'] not in full:continue
        f=full[r['fixture']];first=np.asarray(r['actual'],float);total=np.asarray(f['actual'],float)
        if np.any(first<0) or np.any(first>total):raise ValueError('Invalid half/full labels')
        result.append(dict(fixture=r['fixture'],date=r['date'],shares=r['shares'],first=first.tolist(),total=total.tolist()))
    return result

def shifted_shares(shares,offsets,metric='GOALS_1H'):
    s=np.asarray(shares,float)
    if s.shape!=(2,) or not np.isfinite(s).all() or np.any(s<0) or np.any(s>1):raise ValueError('Invalid half shares')
    if metric=='GOALS_2H':s=1-s
    e=np.exp(np.asarray(offsets,float));shifted=s*e/(1-s+s*e)
    return (1-shifted if metric=='GOALS_2H' else shifted).tolist()

def fit_offsets(rows):
    goals=float(sum(sum(r['total']) for r in rows))
    default=dict(offsets=[0.,0.],fixtures=len(rows),goals=goals,fitted=False,penalty=OFFSET_PENALTY)
    if len(rows)<OFFSET_MIN_FIXTURES or goals<200:return default
    eta=logit(np.clip(np.asarray([r['shares'] for r in rows]),1e-12,1-1e-12))
    first=np.asarray([r['first'] for r in rows]);total=np.asarray([r['total'] for r in rows])
    def loss(offset):
        z=eta+offset
        value=float(np.sum(total*np.logaddexp(0,z)-first*z)+.5*OFFSET_PENALTY*(offset@offset))
        gradient=np.sum(total*expit(z)-first,axis=0)+OFFSET_PENALTY*offset
        return value,gradient
    fitted=minimize(loss,[0.,0.],jac=True,bounds=[(-1.,1.),(-1.,1.)],method='L-BFGS-B')
    if not fitted.success:raise ValueError('Half-share calibration failed: '+fitted.message)
    return dict(default,offsets=fitted.x.tolist(),fitted=True)

def conditional_loss(rows,offsets):
    first=np.asarray([r['first'] for r in rows]);total=np.asarray([r['total'] for r in rows])
    if not len(rows) or total.sum()<=0:raise ValueError('No goals for conditional selection')
    shares=np.asarray([shifted_shares(r['shares'],offsets) for r in rows])
    shares=np.clip(shares,1e-12,1-1e-12)
    return float(-np.sum(first*np.log(shares)+(total-first)*np.log1p(-shares))/total.sum())

def compose_row(ft,period,offsets):
    if ft['fixture']!=period['fixture'] or ft['regime']!=period['regime']:raise ValueError('Inconsistent goal components')
    if period['metric'] not in ('GOALS_1H','GOALS_2H'):raise ValueError('Expected half-period prediction')
    shares=shifted_shares(period['shares'],offsets,period['metric'])
    return dict(period,fullMeans=list(ft['means']),shares=shares,
        means=[m*s for m,s in zip(ft['means'],shares)])

def load_rows(args,name,day,kind):return read_gz(location(args,name)/(name+'_'+day+'_'+kind+'.json.gz'))

def final_profile(args,name,day,mode):
    saved=json.loads((location(args,name)/(name+'_'+day+'_parameters.json')).read_text())
    # Reuse the exact saved full-month profile, including its numerical fit.
    return profile_mode({k:v for k,v in saved.items() if k.startswith('GOALS_FT::')},mode)

def select(args,signature):
    folds=[];source_hashes={}
    for fitday,testday,endday in FOLDS:
        start,end=timestamp(fitday),timestamp(testday)
        datasets={n:load_rows(args,n,testday,'calibration') for n in candidate_names()}
        datasets.update({n:load_rows(args,n,testday,'calibration') for n in FT_BASES if n not in datasets})
        for n in datasets:
            p=location(args,n)/(n+'_'+testday+'_calibration.json.gz')
            source_hashes[n+'_'+testday]=hashlib.sha256(p.read_bytes()).hexdigest()
        boundary,_,_=split_calibration(datasets['full'],start,end)
        ft_options=[];ft_scores={}
        ft_common=set.intersection(*({r['fixture'] for r in datasets[n] if r['metric']=='GOALS_FT' and boundary<=r['date']<end} for n in FT_BASES))
        for name in FT_BASES:
            earlier=[r for r in datasets[name] if r['metric']=='GOALS_FT' and start<=r['date']<boundary]
            later=[r for r in datasets[name] if r['metric']=='GOALS_FT' and boundary<=r['date']<end and r['fixture'] in ft_common]
            profile=fit_joint_profile(earlier)
            for mode in MODES:
                events=validation_events(later,profile_mode(profile,mode))
                score=float(np.mean([(r['q']-r['y'])**2 for r in events]));ft_options.append((score,name,mode))
                ft_scores[name+'::'+mode]=dict(brier=score,events=len(events),fixtures=len(ft_common))
        best_ft=min(ft_options,key=lambda v:v[0]);period_options=[];period_scores={}
        all_records={n:records(datasets['full'],datasets[n]) for n in candidate_names()}
        period_common=set.intersection(*({r['fixture'] for r in rows if boundary<=r['date']<end} for rows in all_records.values()))
        for name in candidate_names():
            earlier=[r for r in all_records[name] if start<=r['date']<boundary]
            later=[r for r in all_records[name] if boundary<=r['date']<end and r['fixture'] in period_common]
            calibration=fit_offsets(earlier)
            for mode in ('identity','venue_offset'):
                offsets=[0.,0.] if mode=='identity' else calibration['offsets']
                score=conditional_loss(later,offsets);period_options.append((score,name,mode))
                period_scores[name+'::'+mode]=dict(conditionalLogLossPerGoal=score,fixtures=len(later),
                    goals=sum(sum(r['total']) for r in later),offsetCalibration=calibration if mode=='venue_offset' else None)
        best_period=min(period_options,key=lambda v:v[0]);name,mode=best_period[1:]
        final=fit_offsets([r for r in all_records[name] if start<=r['date']<end]) if mode=='venue_offset' else dict(offsets=[0.,0.],fitted=False)
        fold=dict(testFrom=testday,testBefore=endday,calibrationFrom=fitday,selectionFromUTC=boundary,selectionBefore=testday,
            primaryFT=dict(candidate='full',mode='joint',lockedToExistingProfile=True),
            experimentalFT=dict(candidate=best_ft[1],mode=best_ft[2],validationBrier=best_ft[0]),
            period=dict(candidate=name,mode=mode,validationConditionalLogLoss=best_period[0],finalCalibration=final),
            ftValidationScores=ft_scores,periodValidationScores=period_scores,
            maxSelectionTimestamp=max(r['date'] for rows in all_records.values() for r in rows if boundary<=r['date']<end))
        if fold['maxSelectionTimestamp']>=end:raise ValueError('Selection touched test labels')
        folds.append(fold)
        print('GOAL_CHOICES',testday,json.dumps({k:fold[k] for k in ('primaryFT','experimentalFT','period')}),flush=True)
    write(args.output/'goal_factorized_choices.json',dict(signature=signature,folds=folds,status='COMPLETE',
        sourceCalibrationSha256=source_hashes,ftCriterion='FT fixed-grid Brier only',
        periodCriterion='Conditional binomial cross-entropy per goal, on common prior-window fixtures',
        protocol='First 2/3 prior month fits profiles/venue offsets; last 1/3 selects; entire prior month fits final offsets',
        primary='Lock existing full-goal distribution exactly; select only conditional half split',
        experimental='Independently select FT base/calibration, without half scores influencing FT choice',
        offsetPenalty=OFFSET_PENALTY,testLabelsUsedForSelection=False,untouchedHoldout=False))
    persist(args.output,'Fourth research cycle: three separate FT/half choice sets frozen; evaluation pending')

def evaluate(args,signature):
    selection=json.loads((args.output/'goal_factorized_choices.json').read_text())
    if selection['signature']!=signature or selection['status']!='COMPLETE':raise ValueError('Choices not frozen')
    paired={arm:[] for arm in ARMS};folds=[];ft_max=0.
    for fold in selection['folds']:
        day=fold['testFrom'];baseline=read_gz(args.baseline/('full_'+day+'.json.gz'))['events']
        base={event_key(r):r for r in baseline if r['metric'] in GOALS}
        periods=load_rows(args,fold['period']['candidate'],day,'predictions')
        period_rows=[r for r in periods if r['metric'] in ('GOALS_1H','GOALS_2H')]
        offsets=fold['period']['finalCalibration']['offsets'];fold_scores={}
        for arm in ARMS:
            choice=fold['primaryFT' if arm==ARMS[0] else 'experimentalFT']
            base_fixtures={r['fixture'] for r in base.values() if r['metric']=='GOALS_FT'}
            full_rows=[r for r in load_rows(args,choice['candidate'],day,'predictions') if r['metric']=='GOALS_FT' and r['fixture'] in base_fixtures]
            full={r['fixture']:r for r in full_rows};profile=final_profile(args,choice['candidate'],day,choice['mode'])
            rows=full_rows+[compose_row(full[r['fixture']],r,offsets) for r in period_rows if r['fixture'] in full]
            events=validation_events(rows,profile);sample=[]
            if set(map(event_key,events))!=set(base):raise ValueError('Goal evaluation cohort changed')
            for r in events:
                b=base[event_key(r)]
                if b['y']!=r['y']:raise ValueError('Paired goal labels changed')
                if arm==ARMS[0] and r['metric']=='GOALS_FT':
                    ft_max=max(ft_max,abs(r['q']-b['q']))
                    if ft_max>1e-12:raise ValueError('Locked FT distribution changed')
                    r=dict(r,q=b['q'])
                sample.append(dict(r,p=b['q']))
            paired[arm].extend(sample);fold_scores[arm]=family_scores(sample)
            zipped(args.output/('goal_factorized_'+arm+'_'+day+'_predictions.json.gz'),rows)
        folds.append(dict(testFrom=day,choices={k:fold[k] for k in ('primaryFT','experimentalFT','period')},scores=fold_scores))
        print('GOAL_EVALUATED',day,flush=True)
    inherited=json.loads((args.output/'regularization_comparison.json').read_text())
    result=dict(signature=signature,folds=folds,goalScores={a:family_scores(r) for a,r in paired.items()},
        inheritedCountScores={m:s for m,s in inherited['selectedVsFull'].items() if m not in GOALS},
        inheritedCountMetricsUnchanged=True,lockedFTMaxCalculatedDifference=ft_max,
        lockedFTStoredScoresExactlyBaseline=True,untouchedHoldout=False,pricedBets=0,providerCalls=0,liveChanges=0,
        limitations=['Inspected development periods','Protocol designed after observing the earlier FT calibration regression',
            'Marginal fixture-bootstrap intervals; no multiplicity or serial-dependence adjustment',
            'Conditional half-split evaluation uses prior actual FT goals only for historical selection, never future prediction',
            'Unpriced event grids do not certify betting tiers or profitability'])
    write(args.output/'goal_factorized_comparison.json',result)
    for arm,rows in paired.items():zipped(args.output/('goal_factorized_'+arm+'_events.json.gz'),rows)
    print('GOAL_COMPLETE',json.dumps({a:{m:round(s['candidate']['brier']-s['baseline']['brier'],8) for m,s in v.items()}
        for a,v in result['goalScores'].items()}),flush=True)
    persist(args.output,'Completed fourth development study: FT anchor plus separately selected conditional half model; future freeze pending')

def family_scores(rows):
    grouped=defaultdict(list)
    for r in rows:grouped[r['metric']].append(r)
    return {m:summary(v) for m,v in grouped.items()}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--stage',choices=('select','evaluate','all'),default='all');args=ap.parse_args()
    data_sha=json.loads((args.baseline/'inventory.json').read_text())['dataSha256']
    source_sha=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();signature=hashlib.sha256((data_sha+source_sha).encode()).hexdigest()
    write(args.output/'goal_factorized_inventory.json',dict(signature=signature,dataSha256=data_sha,sourceSha256=source_sha,
        folds=FOLDS,arms=ARMS,primaryArm=ARMS[0],newRegressionFits=6,ftCandidates=FT_BASES,coherence='One FT joint PMF; complementary binomial thinning',
        futureOutcomesUsed=False,untouchedHoldout=False,liveChanges=0))
    if args.stage in ('select','all'):select(args,signature)
    if args.stage in ('evaluate','all'):evaluate(args,signature)

if __name__=='__main__':main()
