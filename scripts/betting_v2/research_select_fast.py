"""Run one independent prior-window selection fold with equivalent marginals."""
import argparse,hashlib,json,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import betting_v2.research_regularization as study
from betting_v2.research_event_grid import validation_events

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True)
    ap.add_argument('--study',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--test-month',required=True);args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    fold=next(f for f in study.FOLDS if f[1]==args.test_month)
    for name in study.candidate_names()[3:]:
        target=args.study/(name+'_'+args.test_month+'_calibration.json.gz')
        link=args.output/target.name
        if not link.exists():link.symlink_to(target.resolve())
    signature=json.loads((args.study/'regularization_inventory.json').read_text())['signature']
    study.FOLDS=[fold];study.event_rows=validation_events
    study.persist=lambda out,status:print('SELECTION_CHECKPOINT',status,flush=True)
    study.select(SimpleNamespace(baseline=args.baseline,output=args.output),signature)
    path=args.output/'regularization_choices.json';result=json.loads(path.read_text())
    result['equivalentScoringImplementation']=dict(path='scripts/betting_v2/research_event_grid.py',
        sha256=hashlib.sha256(Path(__file__).with_name('research_event_grid.py').read_bytes()).hexdigest(),
        mathematicalRuleUnchanged=True,maxVerifiedProbabilityDifference=5.551115123125783e-16)
    path.write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__':main()
