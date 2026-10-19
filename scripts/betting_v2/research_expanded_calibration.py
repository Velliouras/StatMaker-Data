"""Chronological calibration on the full recovered count-market forecast frame."""
import argparse,sys,json
from pathlib import Path
from collections import defaultdict,Counter
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import ROOT,PRIOR,FOLDS
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_git_archive import absolute_time,COUNT_METRICS,settle_selected
from betting_v2.reliable_counts import metric_values
from betting_v2.audit_probability_trial import canonical_summary
from betting_v2.expanded_calibration import training_frame,fit_bank,adjusted,arms,proper_scores
from betting_v2.audit_market_first_prices import key,stats as price_stats

SOURCE=ROOT/'reports/betting_v2/market_first_counts_20261018'
KINDS=('control','coupled')

def prices(picks,latest):
    index=defaultdict(list)
    for q in latest:index[key(q)].append(q)
    rows=[];excluded=Counter()
    for p in picks:
        candidates=[q for q in index.get(key(p),[]) if q['observed']>p['observed'] and absolute_time(q['marketUpdatedAtUTC'])>=absolute_time(p['marketUpdatedAtUTC'])]
        unique={(q['odd'],q['observed'],q['marketUpdatedAtUTC'],q['sourceReceipt']) for q in candidates}
        if len(unique)!=1:excluded['noLaterExactContract' if not candidates else 'ambiguousLatestQuote']+=1;continue
        q=candidates[0];opposite=list(q['spec']);opposite[2]='UNDER' if opposite[2]=='OVER' else 'OVER'
        other=index.get((q['fixture'],str(q['providerEventId']),q['bookmaker'],tuple(opposite)),[])
        prices={r['odd'] for r in other if r['sourceReceipt']==q['sourceReceipt'] and r['observed']==q['observed'] and r['marketUpdatedAtUTC']==q['marketUpdatedAtUTC']};fair=None
        if len(prices)==1:
            margin=1/q['odd']+1/next(iter(prices))
            if .95<=margin<=1.25:fair=(1/q['odd'])/margin
        rows.append(dict(fixture=p['fixture'],metric=p['spec'][0],providerEventId=p['providerEventId'],bookmaker=p['bookmaker'],spec=p['spec'],firstOdd=p['odd'],latestOdd=q['odd'],firstObserved=p['observed'],latestObserved=q['observed'],priceRatio=p['odd']/q['odd']-1,ownLatestConditionalFair=fair,firstOddTimesLatestFairMinusOne=None if fair is None else p['odd']*fair-1))
    return dict(fixedEarliestPicksIncludingUnknownOutcomes=len(picks),**price_stats(rows),excluded=dict(excluded),rows=rows)


def main(out):
    manifest=read(out/'input_bindings.json');bad=[p for p,h in manifest['files'].items() if not (ROOT/p).exists() or digest((ROOT/p).read_bytes())!=h]
    if bad:raise ValueError('Changed frozen expanded-calibration inputs '+str(bad))
    signature=digest((out/'input_bindings.json').read_bytes());history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};results={};allfits={};first_picks={};coverage={}
    for packet in ('earliest','latest'):
        saved=read(SOURCE/f'replay_checkpoint_{packet}.json.gz');proper=saved['proper'];fits={};frames={kind:training_frame(proper,kind) for kind in KINDS}
        for kind in KINDS:
            fits[kind]={}
            for fold in (*FOLDS[1:],'2026-10-19'):
                cutoff=absolute_time(fold+'T00:00:00Z');fits[kind][fold]={m:fit_bank(frames[kind],m,cutoff) for m in COUNT_METRICS}
                print('EXPANDED_FIT',packet,kind,fold,{m:{k:f['point'][k] for k in ('fixtures','events','fitted','theta')} for m,f in fits[kind][fold].items()},flush=True)
        options=defaultdict(list);eligible_counts=Counter()
        for kind in KINDS:
            for q in saved['options'][kind]:
                valid=[d for d in FOLDS[1:] if absolute_time(d+'T00:00:00Z')<=q['featureCutoff']]
                if not valid:continue
                fold=max(valid);fit=fits[kind][fold][q['spec'][0]]
                if not fit['point']['fitted']:continue
                eligible_counts[kind+'::'+q['spec'][0]]+=1;options[kind+'_raw'].append(q)
                corrected=adjusted(q,fit)
                if corrected is not None:options[kind+'_calibrated'].append(corrected)
        reports={};selected={}
        for kind in KINDS:
            for variant in ('raw','calibrated'):
                for mode,picks in arms(options[kind+'_'+variant]).items():
                    name=kind+'_'+variant+'_'+mode;selected[name]=picks;graded,missing=settle_selected(picks,truth);low=sum(r['y']*r['odd']+r['actualPush']-1 for r in graded)-sum(missing.values());unknown=[q for q in picks if q['fixture'] not in truth or metric_values(truth[q['fixture']],q['spec'][0]) is None]
                    assert len(picks)==len(graded)+len(unknown)
                    high=low+sum(q['odd'] for q in unknown)
                    reports[name]=dict(overall=canonical_summary(graded),selectedBeforeSettlement=len(picks),missingActual=missing,
                        allSelectedProfitBounds=[low,high],allSelectedRoiBounds=[low/len(picks),high/len(picks)] if picks else None,
                        byMetric={m:canonical_summary([r for r in graded if r['spec'][0]==m]) for m in COUNT_METRICS},
                        byFold={d:canonical_summary([r for r in graded if absolute_time(d+'T00:00:00Z')<=r['featureCutoff']<(absolute_time('2026-10-01T00:00:00Z') if d=='2026-09-01' else absolute_time('2026-11-01T00:00:00Z'))]) for d in FOLDS[1:]},picks=graded)
        scores={kind:{fold:{'overall':proper_scores([r for r in proper if r['fold']==fold],kind,fits[kind][fold]),**{m:proper_scores([r for r in proper if r['fold']==fold and r['metric']==m],kind,fits[kind][fold]) for m in COUNT_METRICS}} for fold in FOLDS[1:]} for kind in KINDS}
        write(out/f'priced_{packet}.json.gz',dict(reports=reports,selected=selected,properScores=scores),zipped=True)
        results[packet]=dict(reports={k:{a:b for a,b in v.items() if a!='picks'} for k,v in reports.items()},properScores=scores);allfits[packet]=fits
        coverage[packet]=dict(properEvents=len(proper),properFixtures=len({r['fixture'] for r in proper}),conditionalTrainingEvents={k:len(v) for k,v in frames.items()},eligibleOptionCounts=dict(eligible_counts))
        if packet=='earliest':first_picks=selected
        write(out/'progress.json',dict(stage='EVALUATE',packet=packet,providerCalls=0,llmCalls=0));print('EXPANDED_RESULT',packet,{k:(v['overall']['bets'],v['overall'].get('roi')) for k,v in reports.items()},flush=True)
    price_results={name:prices(picks,read(SOURCE/'quotes_latest.json.gz')) for name,picks in first_picks.items()}
    write(out/'fixed_first_prices.json.gz',price_results,zipped=True);write(out/'parameter_banks.json',dict(fits=allfits,inputSignature=signature,parametersFrozenBeforeEvaluation=True,fullDataFrozenAt='2026-10-19T00:00:00Z',deployed=False))
    summary=dict(status='COMPLETE',primary='coupled_calibrated_conservative',results=results,coverage=coverage,
        priceAudit={k:{a:b for a,b in v.items() if a!='rows'} for k,v in price_results.items()},providerCalls=0,llmCalls=0,baseModelRefits=0,calibratorFitCalls=624,calibratorFits=sum(int(b['point']['fitted'])+sum(int(x['fitted']) for x in b['replicas']) for packet in allfits.values() for kind in packet.values() for fold in kind.values() for b in fold.values()),
        thresholdsChanged=False,roiOptimized=False,untouchedHoldout=False,deployed=False,inputSignature=signature)
    write(out/'summary.json',summary);write(out/'progress.json',dict(stage='COMPLETE',status='COMPLETE',providerCalls=0,llmCalls=0))
    lines=['# Διόρθωση πιθανοτήτων στο πλήρες ανακτημένο δείγμα','','Μόνο prior μήνες στην εκπαίδευση, ίδια όρια, ίδια διαθέσιμα γεγονότα στα raw/calibrated. Όλοι οι μήνες παραμένουν DEVELOPMENT. Πρώτη και τελευταία παραλαβή επικαλύπτονται.','', '| Σκέλος | Πρώτη: bets / ROI | Τελευταία: bets / ROI |','|---|---:|---:|']
    for kind in KINDS:
        for variant in ('raw','calibrated'):
            for mode in ('point','conservative'):
                arm=kind+'_'+variant+'_'+mode;cells=[]
                for packet in ('earliest','latest'):
                    s=results[packet]['reports'][arm]['overall'];cells.append(str(s['bets'])+' / '+(f"{s['roi']:+.2%}" if 'roi' in s else '—'))
                lines.append('| '+arm+' | '+' | '.join(cells)+' |')
    lines+=['','Το p>=60%, 1.80<odds<3.00, EV, guards φαβορί και winner-before-conservative παραμένουν αμετάβλητα. Το νέο decision layer δεν αυξάνει raw πιθανότητα για να δημιουργήσει επιλέξιμο bet. Η πλήρης πιθανότητα για proper scoring παραμένει συμπληρωματική OVER/UNDER με σταθερή push mass. Οι συντηρητικές ευαισθησίες δεν είναι εγγυημένα confidence bounds.','', 'Τα πλήρη scores ανά αγορά/μήνα, οι γνωστές και άγνωστες επιλογές, τα όρια ελλειπόντων αποτελεσμάτων και ο ίδιος ακριβής μεταγενέστερος bookmaker/line έλεγχος φυλάσσονται στα artifacts. Καμία αλλαγή ενεργού app ή παλιών prospective αρχείων.']
    (out/'RESULTS_GR.md').write_text('\n'.join(lines)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
