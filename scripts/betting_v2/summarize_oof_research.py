"""Paired development scores, mean-only count alternative, probability bands."""
import argparse,gzip,json,sys
from collections import defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.validated_walk_forward import FOLDS,grid,distribution
from betting_v2.recovery_walk_forward import summary,write
from betting_v2.summarize_corrected import load_variant

def key(r):return r['fixture'],r['metric'],r['market']
def family_scores(rows):
    grouped=defaultdict(list)
    for r in rows:grouped[r['metric']].append(r)
    return {m:summary(v) for m,v in grouped.items()}
def bands(rows):
    result={}
    for metric in sorted({r['metric'] for r in rows}):
        result[metric]={}
        for lo,hi,label in ((.6,.7,'60-70'),(.7,.8,'70-80'),(.8,1.000001,'80-100')):
            sample=[r for r in rows if r['metric']==metric and lo<=r['q']<hi]
            if not sample:continue
            groups=defaultdict(list)
            for r in sample:groups[r['fixture']].append(r['y'])
            sums=np.array([sum(v) for v in groups.values()]);counts=np.array([len(v) for v in groups.values()])
            rng=np.random.default_rng(15102026);boot=[]
            for _ in range(1000):
                ix=rng.integers(len(sums),size=len(sums));boot.append(sums[ix].sum()/counts[ix].sum())
            result[metric][label]=dict(events=len(sample),fixtures=len(groups),predicted=float(np.mean([r['q'] for r in sample])),
                observed=float(np.mean([r['y'] for r in sample])),observed95=list(map(float,np.quantile(boot,[.025,.975]))))
    return result
def mean_only(calibration):
    grouped=defaultdict(list)
    for r in calibration:
        if r['metric'] not in ('GOALS_1H','GOALS_2H'):grouped[r['metric']+'::'+r['regime']].append(r)
    result={}
    for k,rows in grouped.items():
        scale=sum(sum(r['actual']) for r in rows)/sum(sum(r['means']) for r in rows) if len(rows)>=100 else 1.
        result[k]=dict(scale=float(np.clip(scale,np.exp(-.7),np.exp(.7))),alpha=0.,n=len(rows))
    return result
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();out=args.output;baseline,_=load_variant(args.baseline,'full');candidate,_=load_variant(out,'oof_goal_stack')
    index={key(r):r for r in baseline};raw=[];cal=[];goal_delta=[]
    for r in candidate:
        b=index.get(key(r))
        if b is None:continue
        if b['y']!=r['y']:raise ValueError('Label mismatch')
        raw.append(dict(r,p=b['p'],q=r['p']));cal.append(dict(r,p=b['q'],q=r['q']))
        if r['metric']=='GOALS_FT':goal_delta.append(abs(b['p']-r['p']))
    if max(goal_delta,default=0)>1e-12:raise ValueError('Anchored goal fit changed')
    print('PAIRED',len(raw),flush=True)
    result=dict(commonEvents=len(raw),baselineEvents=len(baseline),oofEvents=len(candidate),
        rawOofVsInSample=family_scores(raw),calibratedOofVsInSample=family_scores(cal),
        calibratedOofBands=bands(candidate),calibratedBaselineBands=bands(baseline),
        goalBaseMaxAbsoluteDifference=max(goal_delta,default=0),untouchedHoldout=False,promotionAllowed=False,
        bandMeaning='Unpriced fixed event grid, not priced-selection tiers; clustered by fixture',
        inference='Marginal descriptive intervals, without multiple-testing adjustment')
    write(out/'model_comparison.json',result);print('OOF_SUMMARIZED',flush=True)
    poisson=[];profiles={}
    for _,day,_ in FOLDS:
        stem='full_'+day
        calibration=json.loads(gzip.decompress((args.baseline/(stem+'_calibration.json.gz')).read_bytes()))
        profile=mean_only(calibration);profiles[day]=profile
        predictions=json.loads(gzip.decompress((args.baseline/(stem+'_predictions.json.gz')).read_bytes()))
        for r in predictions:
            joint=distribution(r,profile)
            for spec in grid(r['metric']):
                market='|'.join(map(str,spec[1:]));b=index.get((r['fixture'],r['metric'],market))
                if b is not None:
                    win,push=joint.evaluate(spec);poisson.append(dict(b,p=b['q'],q=win/(1-push)))
        print('MEAN_ONLY',day,flush=True)
    write(out/'poisson_mean_profiles.json',profiles);result['poissonMeanVsJointNB']=family_scores(poisson)
    write(out/'model_comparison.json',result)
    lines=['| Family | OOF raw ΔBrier | OOF calibrated ΔBrier | Mean-Poisson vs joint-NB ΔBrier |','|---|---:|---:|---:|']
    delta=lambda s:s['candidate']['brier']-s['baseline']['brier']
    for m,s in result['rawOofVsInSample'].items():
        lines.append(f"| {m} | {delta(s):+.6f} | {delta(result['calibratedOofVsInSample'][m]):+.6f} | {delta(result['poissonMeanVsJointNB'][m]):+.6f} |")
    (out/'MODEL_METRICS.md').write_text('\n'.join(lines)+'\n');print('\n'.join(lines),flush=True)
if __name__=='__main__':main()
