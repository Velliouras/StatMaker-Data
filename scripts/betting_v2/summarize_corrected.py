"""Paired fixture-cluster uncertainty; no promotion from inspected test scores."""
import argparse,gzip,json,sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.recovery_walk_forward import summary,write


def load_variant(out,variant):
    rows=[];folds=[]
    for day in ('2026-08-01','2026-09-01','2026-10-01'):
        path=out/(variant+'_'+day+'.json.gz')
        if not path.exists():raise ValueError('Missing completed fold '+str(path))
        d=json.loads(gzip.decompress(path.read_bytes()));folds.append({k:v for k,v in d.items() if k!='events'})
        rows.extend(d['events'])
    return rows,folds


def summarize(rows):
    bymarket=defaultdict(list);byfamily=defaultdict(list)
    for r in rows:
        bymarket[r['metric']+'::'+r['regime']+'::'+r['market']].append(r)
        byfamily[r['metric']].append(r)
    return dict(byMarket={k:summary(v) for k,v in bymarket.items()},byFamily={k:summary(v) for k,v in byfamily.items()})


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--variants',nargs='+',default=['full','no_xg','no_goal_stack']);args=ap.parse_args();out=args.output
    allrows={};folds={};results={}
    for variant in args.variants:
        rows,f=load_variant(out,variant);allrows[variant]=rows;folds[variant]=f
        print('SUMMARIZE',variant,len(rows),flush=True);results[variant]=summarize(rows)
    def key(r):return r['fixture'],r['metric'],r['market']
    base={key(r):r for r in allrows['full']};ablations={}
    for variant,rows in allrows.items():
        if variant=='full':continue
        paired=[]
        for r in rows:
            b=base.get(key(r))
            if b is not None:
                assert b['y']==r['y']
                # Compare raw predictions to isolate features from calibration.
                paired.append(dict(r,p=b['p'],q=r['p']))
        ablations[variant]=dict(commonEvents=len(paired),baselineEvents=len(base),candidateEvents=len(rows),**summarize(paired))
    result=dict(calibrationComparisons=results,rawFeatureAblations=ablations,folds=folds,
        untouchedHoldout=False,productionEnabled=False,providerCalls=0,
        scoreDefinition='Binary event grid; DNB Brier/log loss conditional on non-push, other markets unconditional',
        uncertainty='Paired bootstrap by fixture; marginal descriptive intervals, no multiple-testing correction',
        baseline='Corrected UTC/deduplicated V3 architecture with day-lagged rolling state; not old corrupted deployed probabilities',
        limitations=['Cached final statistics have no verified historic publication times',
            'Frozen coefficients updated monthly, rolling features daily; not daily refit replay',
            'In-sample goal-strength stacking retained in full; separately ablated in no_goal_stack',
            'Calibration periods and test periods previously inspected; no pristine holdout claim',
            'Counts and event grids do not establish bookmaker card or foul settlement contracts'])
    write(out/'comparison.json',result)
    table=['| Family | Baseline Brier | Calibrated Brier | Relative reduction | Paired delta 95% |',
           '|---|---:|---:|---:|---|']
    for m,s in results['full']['byFamily'].items():
        b=s['baseline']['brier'];q=s['candidate']['brier'];ci=s['pairedBrierDelta95']
        table.append(f'| {m} | {b:.6f} | {q:.6f} | {(1-q/b)*100:.2f}% | [{ci[0]:.6f}, {ci[1]:.6f}] |')
    (out/'METRICS.md').write_text('\n'.join(table)+'\n')
    print('\n'.join(table),flush=True)

if __name__=='__main__':main()
