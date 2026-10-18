"""Compare fixed-winner abstention with its exact pre-existing raw selectors."""
import argparse,hashlib,json,sys
from collections import Counter
from pathlib import Path
from datetime import datetime,timezone
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_scope_expansion import select_scope
from betting_v2.research_git_archive import absolute_time,settle_selected
from betting_v2.audit_probability_trial import canonical_summary
from betting_v2.winner_calibration import bank,decide,adjusted

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'reports/betting_v2/probability_recalibration_20261018'
FOLDS=('2026-08-01','2026-09-01','2026-10-01')


def proper(rows,parameters):
    rows=[r for r in rows if not r['actualPush']]
    if not rows:return dict(events=0,fixtures=0)
    counts=Counter(r['fixture'] for r in rows);w=np.asarray([1/counts[r['fixture']] for r in rows]);w/=w.sum()
    y=np.asarray([r['y'] for r in rows]);raw=np.asarray([r['p']/(1-r['push']) for r in rows])
    calibrated=np.asarray([adjusted(r['p'],r['push'],parameters['point']['bias'])/(1-r['push']) for r in rows])
    def score(p):
        p=np.clip(p,1e-12,1-1e-12)
        return dict(logLoss=float(w@(-y*np.log(p)-(1-y)*np.log1p(-p))),brier=float(w@((p-y)**2)),
            meanProbability=float(w@p),eventRate=float(w@y))
    return dict(events=len(rows),fixtures=len(counts),raw=score(raw),calibrated=score(calibrated))


def fold(cutoff):
    eligible=[d for d in FOLDS if absolute_time(d+'T00:00:00Z')<=cutoff]
    if not eligible:raise ValueError('Winner predates every frozen fold')
    return max(eligible)


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();out=args.output
    binding=read(out/'input_bindings.json')
    changed=[p for p,h in binding['files'].items() if digest((ROOT/p).read_bytes())!=h]
    if changed:raise ValueError('Changed fixed inputs '+str(changed))
    history=read(ROOT/'reports/betting_v2/prior_market_calibration_20261018/history.json.gz')
    truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    results={};all_banks={}
    for packet in ('earliest','latest'):
        source=read(SOURCE/f'checkpoint_{packet}.json.gz');reports={};fits={};proper_scores={};reproduction={}
        previous=read(SOURCE/f'priced_{packet}.json')
        for policy in ('point','conservative'):
            # Freeze the original winner before reading any current actual.
            winners=select_scope(source['options']['base_'+policy])
            if len({(r['fixture'],r['spec'][0]) for r in winners})!=len(winners):raise ValueError('Duplicate raw winner')
            scored,missing=settle_selected(winners,truth)
            original=previous['reports']['base_'+policy]
            if canonical_summary(scored)!=canonical_summary(original['picks']):raise ValueError('Raw winners not reproduced')
            reproduction[policy]=dict(selected=len(winners),scored=len(scored),missing=missing)
            banks={d:bank(scored,absolute_time(d+'T00:00:00Z')) for d in FOLDS}
            fits[policy]=banks
            matched=[];selected=[];month_scores={}
            for winner in winners:
                day=fold(winner['featureCutoff']);parameters=banks[day]
                if not parameters['point']['fitted']:continue
                matched.append(winner)
                result=decide(winner,parameters,policy=='conservative')
                if result is not None:selected.append(result)
            for name,rows in [('matched_'+policy,matched),('fixed_winner_'+policy,selected)]:
                score,missing=settle_selected(rows,truth)
                bymonth={d:canonical_summary([r for r in score if datetime.fromtimestamp(r['kickoff'],timezone.utc).strftime('%Y-%m')==d[:7]]) for d in FOLDS}
                reports[name]=dict(overall=canonical_summary(score),selectedBeforeSettlement=len(rows),missingActual=missing,
                    byMonth=bymonth,byMetric={m:canonical_summary([r for r in score if r['spec'][0]==m]) for m in ('CORNERS','SHOTS','SOT','FOULS')},
                    picks=score,abstentions=len(matched)-len(selected) if name.startswith('fixed_') else 0)
            for day in FOLDS:
                if banks[day]['point']['fitted']:
                    month_scores[day]=proper([r for r in scored if fold(r['featureCutoff'])==day],banks[day])
            proper_scores[policy]=month_scores
        write(out/f'priced_{packet}.json',dict(reports=reports,calibrationBanks=fits,properScores=proper_scores,
            baseReproduction=reproduction,providerCalls=0,llmCalls=0,rateRefits=0,untouchedHoldout=False,
            thresholdChanges=False,runnerUpFallback=False,deployed=False))
        results[packet]=dict(reports={k:{a:b for a,b in v.items() if a!='picks'} for k,v in reports.items()},properScores=proper_scores,baseReproduction=reproduction)
        all_banks[packet]=fits
        print('FIXED_WINNER_RESULT',packet,{k:v['overall'] for k,v in reports.items()},flush=True)
    write(out/'calibration_banks.json',dict(banks=all_banks,deployed=False,sourceSignature=digest((out/'input_bindings.json').read_bytes())))
    write(out/'summary.json',dict(status='COMPLETE',results=results,thresholdChanges=False,runnerUpFallback=False,
        providerCalls=0,llmCalls=0,rateRefits=0,deployed=False,untouchedHoldout=False))


if __name__=='__main__':main()
