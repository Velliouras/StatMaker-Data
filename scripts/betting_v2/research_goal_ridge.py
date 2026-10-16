"""Fit stronger FT goal regressions without refitting other market families."""
import argparse,copy,gzip,hashlib,json,sys,time
from itertools import groupby
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.opponent_matchup import OpponentMatchup,timestamp
from betting_v2.chronological_stack import initialize,matrix
from betting_v2.monotone_count import fit_monotone_count
from betting_v2.validated_walk_forward import FOLDS,rolling_rows,export_state,zipped
from betting_v2.training_protocol import fit_joint_profile
from betting_v2.research_regularization import read_gz
from betting_v2.research_event_grid import validation_events
from betting_v2.recovery_walk_forward import write
from betting_v2.research_checkpoint import persist

FACTORS=(10.,50.)

def prior_design(history,cutoff,metric_values):
    model=OpponentMatchup.__new__(OpponentMatchup);initialize(model)
    for _,batch in groupby((r for r in history if r['_date']<cutoff),lambda r:int(r['_date']//86400)):
        batch=list(batch)
        for row in batch:
            h,a=row['_home'],row['_away'];model.groups[h],model.groups[a]=row['_home_group'],row['_away_group']
            y=metric_values(row,'GOALS_FT')
            if y is not None:
                model.train['GOALS_FT'].extend((model.features(t,o,'GOALS_FT',v),n)
                    for t,o,v,n in ((h,a,True,y[0]),(a,h,False,y[1])))
            model.matches+=1
        for row in batch:model.observe(row,metric_values)
    records=model.train['GOALS_FT'];x,columns,scales=matrix([f for f,_ in records])
    return model,x,np.asarray([y for _,y in records]),columns,scales

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    history=direct.load_actual_results(timestamp('2026-10-15'))['GLOBAL']
    data_sha=hashlib.sha256(json.dumps(history,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if data_sha!=json.loads((args.baseline/'inventory.json').read_text())['dataSha256']:raise ValueError('Different baseline history')
    signature=hashlib.sha256((data_sha+hashlib.sha256(Path(__file__).read_bytes()).hexdigest()).encode()).hexdigest()
    write(args.output/'goal_ridge_inventory.json',dict(signature=signature,dataSha256=data_sha,factors=FACTORS,folds=FOLDS,
        noGoalAnchor=True,metric='GOALS_FT only',newFits=6,providerCalls=0,productionEnabled=False,untouchedHoldout=False))
    for i,(fitday,testday,endday) in enumerate(FOLDS):
        names=[f'goal_ridge_{int(f)}' for f in FACTORS]
        if all((args.output/(n+'_'+testday+'.json.gz')).exists() for n in names):
            for n in names:
                if read_gz(args.output/(n+'_'+testday+'.json.gz'))['signature']!=signature:raise ValueError('Different checkpoint source')
            continue
        started=time.time();base,x,y,columns,scales=prior_design(history,timestamp(fitday),direct.metric_values)
        anchor=read_gz(args.baseline/('full_'+testday+'_pretest_state.json.gz'))['weights']['GOALS_FT']
        if columns!=anchor['columns'] or not np.allclose(scales,anchor['scales'],rtol=0,atol=1e-12):raise ValueError('Different baseline goal design')
        print('GOAL_DESIGN',testday,len(y),len(columns),flush=True)
        for factor,name in zip(FACTORS,names):
            stem=name+'_'+testday;model=copy.deepcopy(base)
            model.columns['GOALS_FT']=columns;model.feature_scales['GOALS_FT']=scales
            model.models['GOALS_FT']=fit_monotone_count(x,y,columns,'GOALS_FT',2.*factor/len(y))
            calibration=rolling_rows(model,history,timestamp(fitday),timestamp(testday));profile=fit_joint_profile(calibration)
            zipped(args.output/(stem+'_calibration.json.gz'),calibration)
            zipped(args.output/(stem+'_pretest_state.json.gz'),export_state(model));write(args.output/(stem+'_parameters.json'),profile)
            later=rolling_rows(model,history,timestamp(testday),timestamp(endday));zipped(args.output/(stem+'_predictions.json.gz'),later)
            events=validation_events(later,profile)
            zipped(args.output/(stem+'.json.gz'),dict(signature=signature,variant=name,trainBefore=fitday,testFrom=testday,
                testBefore=endday,factor=factor,trainingMatches=base.matches,trainingRows=len(y),events=events))
            print('GOAL_FIT',stem,len(events),round(time.time()-started,1),flush=True)
        persist(args.output,f'Fourth research cycle: stronger FT goal regression {i+1}/3 fold pairs saved; selection pending')

if __name__=='__main__':main()
