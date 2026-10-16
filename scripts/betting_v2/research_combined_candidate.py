"""Coupled count uncertainty + prior-learned goal market calibration.

Proposed after the individual diagnostics, so results remain development data.
No individual market is removed on its test ROI. All goal calibrator weights
come from the preceding market decision experiment, all count profiles from
pre-October statistical labels. Both point and conservative policies retained.
"""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write
from betting_v2.reliable_counts import METRICS
from betting_v2.paired_count import fit_profile
from betting_v2.research_market_decisions import attach_reference
from betting_v2.market_calibration import pooled_probability
from betting_v2.live_revised import load_revised
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.research_app_markets import prepare_extended
from betting_v2.validated_walk_forward import distribution
from betting_v2.research_quotes import assess,stats
from betting_v2.research_scope_expansion import select_scope

ROOT=Path(__file__).resolve().parents[2]


def calibration_banks(out):
    source=ROOT/'reports/betting_v2/all_market_revision_20261016';banks={}
    for metric in METRICS:
        path=out/(metric+'_profiles.json')
        if path.exists():banks[metric]=read(path);continue
        rows=read(source/(metric+'_2026-10-01_calibration.json.gz'))
        values=[fit_profile(rows)]
        for i in range(12):values.append(fit_profile([dict(r,means=r['bootstrapMeans'][i]) for r in rows]))
        write(path,values);banks[metric]=values;print('COMBINED_BANK',metric,flush=True)
    return banks


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--frames',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    banks=calibration_banks(args.output);history=direct.load_actual_results(direct.timestamp('2026-10-16'))['GLOBAL'];results={}
    for packet in ('earliest','latest'):
        rows,_=attach_reference(read(args.frames/('frame_'+packet+'.json.gz'))['rows'])
        fits=read(args.frames/('receipt_'+packet+'_results.json'))['calibration'];options={a:[] for a in ('combined_point','combined_conservative')}
        for cutoff in sorted({r['featureCutoff'] for r in rows}):
            model,old,_=load_revised(ROOT,history,cutoff);model.count_profiles=banks;profile=dict(old)
            for values in banks.values():profile.update(values[0])
            model.revision_profile=profile;cache={}
            for r in rows:
                if r['featureCutoff']!=cutoff or not 1.8<r['odd']<3:continue
                metric=r['spec'][0];h,a=r['_home'],r['_away'];key=h,a,metric
                if key not in cache:
                    pred=model.predict(h,a,metric);full=model.predict(h,a,'GOALS_FT')
                    regime='XG' if min(len(model.xg[h]),len(model.xg[a]))>=8 else 'ELO_NO_XG'
                    target=dict(metric=metric,regime=regime,means=list(pred[:2]))
                    if metric in ('GOALS_1H','GOALS_2H'):target.update(fullMeans=list(full[:2]),shares=[pred[i]/full[i] for i in (0,1)])
                    if metric=='GOALS_FT':
                        half=model.predict(h,a,'GOALS_1H')
                        if half:target['periodShares']=[half[i]/full[i] for i in (0,1)]
                    cache[key]=target,prepare_extended(target,profile),distribution(dict(metric='GOALS_FT',regime=regime,means=list(full[:2])),old)
                pred=cache[key];pick=assess(r,pred,profile,r['fav'],'current_preference')
                if pick is None:continue
                if metric.startswith('GOALS'):
                    ref=r['reference'];fit=fits.get(metric+'::'+str(int(cutoff)))
                    if ref is None or fit is None:continue
                    market_probability=pooled_probability(pick['p'],pick['push'],ref['q'],fit['weight'])
                    p=min(pick['p'],market_probability);ev=p*r['odd']+pick['push']-1
                    if p<.6 or ev<=0:continue
                    pick=dict(pick,p=p,ev=ev,confidence=min(pick['confidence'],p))
                options['combined_point'].append(pick)
                if metric.startswith('GOALS') and r['spec'][1] not in ('RESULT','TOTAL','TEAM','DC','DNB','BTTS'):continue
                b=model.bounds(h,a,r['spec'],r['odd'])
                if b is None:continue
                p=min(pick['p'],b['win']);ev=p*r['odd']+b['push']-1
                if p<.6 or ev<=0:continue
                support=b[r['fav'].lower()] if r['fav'] else None
                if (metric.startswith('GOALS') or metric in ('CORNERS','SHOTS','SOT')) and support is not None and support<.6:continue
                confidence=min(p,support) if support is not None else p
                options['combined_conservative'].append(dict(pick,p=p,push=b['push'],ev=ev,confidence=confidence,
                    tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING'))
        reports={}
        for arm,rows in options.items():
            picks=select_scope(rows);reports[arm]=dict(overall=stats(picks),picks=picks,
                byMetric={m:stats([r for r in picks if r['spec'][0]==m]) for m in sorted({r['spec'][0] for r in picks})})
        write(args.output/('priced_'+packet+'.json'),reports)
        results[packet]={a:{k:v for k,v in r.items() if k!='picks'} for a,r in reports.items()}
        print('COMBINED_RESULT',packet,results[packet],flush=True)
    write(args.output/'summary.json',dict(status='COMPLETE',results=results,providerCalls=0,llmCalls=0,newMeanFits=0,appChanges=0,
        countBootstrapRefits=84,untouchedHoldout=False,roiOptimized=False,
        context='Development combination proposed after the individual diagnostics; prior fits do not make the test periods untouched',
        uncertainty='12 weekly parameter refits plus separately fitted prior total/allocation profiles; not calibrated protection from systematic error',
        rule='All count families kept; goal probability is capped by its previously fitted prior-market calibration, never excluded using test ROI'))

if __name__=='__main__':main()
