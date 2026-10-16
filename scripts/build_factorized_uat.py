"""Create hash-bound UAT metadata from the already selected October research.

Run against the pinned research dataset, never against refreshed live history.
This exports metadata and effective profiles; it does not train any estimator.
"""
import argparse,gzip,hashlib,json
from pathlib import Path
import materialize_betting_v2_direct as direct
from betting_v2.opponent_matchup import timestamp
from betting_v2.research_regularization import profile_mode

ROOT=Path(__file__).resolve().parents[1]
RESEARCH=ROOT/'reports/betting_v2/oof_research_20261016'
OUTPUT=ROOT/'reports/betting_v2/uat_factorized_20261016'
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path,value): path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True);args=ap.parse_args()
    inventory=json.loads((args.baseline/'inventory.json').read_text())
    history=direct.load_actual_results(timestamp('2026-10-15'))['GLOBAL']
    data_sha=hashlib.sha256(json.dumps(history,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if data_sha!=inventory['dataSha256']: raise ValueError('Requires the pinned research history')
    goal_path=RESEARCH/'goal_factorized_choices.json';count_path=RESEARCH/'regularization_choices.json'
    goals=json.loads(goal_path.read_text());counts=json.loads(count_path.read_text())
    if goals['status']!='COMPLETE' or counts['status']!='COMPLETE': raise ValueError('Incomplete selection')
    day='2026-10-01';fit='2026-09-01'
    g=next(f for f in goals['folds'] if f['testFrom']==day)
    c=next(f for f in counts['folds'] if f['testFrom']==day)
    ft=g['experimentalFT']['candidate'];period=g['period']['candidate']
    names={v['candidate'] for k,v in c['choices'].items() if k!='GOALS'}
    if len(names)!=1: raise ValueError('This deployment expects one selected count engine')
    count=names.pop();states={};before=[r for r in history if r['_date']<timestamp(fit)]
    for name in sorted({ft,period,count}):
        path=RESEARCH/(name+'_'+day+'_pretest_state.json.gz')
        state=json.loads(gzip.decompress(path.read_bytes()))
        if state['training']!=len(before): raise ValueError('Training count mismatch')
        samples=dict(metrics={m:sum(direct.metric_values(r,m) is not None for r in before)
            for m in state['weights'] if m!='period'},periodObservations=sum(
            sum(v>0 for v in direct.metric_values(r,'GOALS_FT')) for r in before
            if direct.metric_values(r,'GOALS_1H') is not None))
        states[name]=dict(path=path.relative_to(ROOT).as_posix(),sha256=sha(path),sampleCounts=samples)
    def profile(name): return json.loads((RESEARCH/(name+'_'+day+'_parameters.json')).read_text())
    selected=profile_mode({k:v for k,v in profile(ft).items() if k.startswith('GOALS_FT::')},g['experimentalFT']['mode'])
    for metric,choice in c['choices'].items():
        if metric=='GOALS': continue
        selected.update({k:v for k,v in profile_mode(profile(choice['candidate']),choice['mode']).items()
                         if k.startswith(metric+'::')})
    OUTPUT.mkdir(parents=True,exist_ok=True)
    p=OUTPUT/'effective_profile.json';write(p,selected)
    write(OUTPUT/'deployment.json',dict(schema=1,variant='separate_ft_selected_period',
        fittedBefore=fit,calibrationBefore=day,ft=ft,period=period,counts=count,
        halfOffsets=g['period']['finalCalibration']['offsets'],states=states,
        profile=dict(path=p.relative_to(ROOT).as_posix(),sha256=sha(p)),
        goalSelectionSha256=sha(goal_path),countSelectionSha256=sha(count_path),
        researchDataSha256=data_sha,untouchedHoldout=False,profitabilityValidated=False))
    print('FROZEN_UAT_METADATA',json.dumps(dict(ft=ft,period=period,counts=count)))
if __name__=='__main__': main()
