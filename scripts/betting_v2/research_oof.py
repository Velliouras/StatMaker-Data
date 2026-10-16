"""Resumable, research-only chronological stacking with a fixed outer goal fit."""
import argparse,gzip,hashlib,json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.opponent_matchup import timestamp
from betting_v2.chronological_stack import ChronologicalStack,PriorGoalBank
from betting_v2.validated_walk_forward import FOLDS,rolling_rows,event_rows,export_state,zipped
from betting_v2.training_protocol import fit_joint_profile
from betting_v2.recovery_walk_forward import write
from betting_v2.research_checkpoint import persist

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--baseline',type=Path,required=True)
    args=ap.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    fd=os.open(out/'running.lock',os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.write(fd,str(os.getpid()).encode());os.close(fd)
    try:
        history=direct.load_actual_results(timestamp('2026-10-15'))['GLOBAL']
        data_sha=hashlib.sha256(json.dumps(history,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        baseline=json.loads((args.baseline/'inventory.json').read_text())
        if data_sha!=baseline['dataSha256']:raise ValueError('Different baseline dataset')
        sources=[Path(__file__),Path(__file__).with_name('chronological_stack.py')]
        signature=hashlib.sha256((data_sha+hashlib.sha256(b''.join(p.read_bytes() for p in sources)).hexdigest()).encode()).hexdigest()
        write(out/'inventory.json',dict(signature=signature,dataSha256=data_sha,matches=len(history),folds=FOLDS,
            variant='oof_goal_stack',providerCalls=0,untouchedHoldout=False,productionEnabled=False,
            outerGoalFit='identical saved baseline weights',innerCutoffs='UTC month start',coldStartRule='zero gap before 200 prior matches'))
        bank=PriorGoalBank(history,direct.metric_values)
        old=out/'diagnostic_bank.json.gz'
        if old.exists():bank.cache={float(k):v for k,v in json.loads(gzip.decompress(old.read_bytes())).items()}
        def save_bank(bank):zipped(out/'prior_goal_bank.json.gz',bank.cache)
        bank.on_fit=save_bank
        if (out/'prior_goal_bank.json.gz').exists():bank.cache={float(k):v for k,v in json.loads(gzip.decompress((out/'prior_goal_bank.json.gz').read_bytes())).items()}
        for i,(fitday,testday,endday) in enumerate(FOLDS):
            stem='oof_goal_stack_'+testday;checkpoint=out/(stem+'.json.gz')
            if checkpoint.exists():
                d=json.loads(gzip.decompress(checkpoint.read_bytes()))
                if d['signature']!=signature:raise ValueError('Checkpoint fingerprint mismatch')
                print('RESUME',stem,flush=True);continue
            start=time.time();print('FIT',stem,flush=True)
            anchor=json.loads(gzip.decompress((args.baseline/('full_'+testday+'_pretest_state.json.gz')).read_bytes()))['weights']['GOALS_FT']
            model=ChronologicalStack(history,timestamp(fitday),direct.metric_values,bank=bank,goal_anchor=anchor)
            cal=rolling_rows(model,history,timestamp(fitday),timestamp(testday));profile=fit_joint_profile(cal)
            zipped(out/(stem+'_pretest_state.json.gz'),export_state(model));zipped(out/(stem+'_calibration.json.gz'),cal)
            write(out/(stem+'_parameters.json'),profile);print('CALIBRATED',stem,len(cal),flush=True)
            rows=rolling_rows(model,history,timestamp(testday),timestamp(endday));zipped(out/(stem+'_predictions.json.gz'),rows)
            events=event_rows(rows,profile)
            zipped(checkpoint,dict(signature=signature,events=events,variant='oof_goal_stack',trainBefore=fitday,testFrom=testday,
                testBefore=endday,training=model.matches,seconds=time.time()-start))
            write(out/'chronology_audit.json',dict(bankModels=[{k:v for k,v in b.items() if k not in ('coef','columns')} for b in bank.cache.values()],coldStartFixtures=len(bank.cold_fixtures)))
            print('CHECKPOINT',stem,len(events),round(time.time()-start,1),flush=True)
            persist(out,f'OOF research: {i+1}/3 chronological folds completed; analysis pending')
        print('COMPLETE',flush=True)
    finally:(out/'running.lock').unlink(missing_ok=True)
if __name__=='__main__':main()
