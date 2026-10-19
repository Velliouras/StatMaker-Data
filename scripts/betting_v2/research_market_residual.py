"""Learn incremental count information relative to same-receipt fair markets."""
import argparse,sys
from pathlib import Path
from collections import Counter,defaultdict
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import ROOT,PRIOR,FOLDS
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_git_archive import absolute_time,COUNT_METRICS,settle_selected
from betting_v2.market_calibration import references
from betting_v2.market_residual import bank,predict,candidate,SEED
from betting_v2.contract_forecasts import restore
from betting_v2.expanded_calibration import arms
from betting_v2.audit_probability_trial import canonical_summary
from betting_v2.research_expanded_calibration import prices
SOURCE=ROOT/'reports/betting_v2/market_first_counts_20261018'
KINDS=('stat','market','residual')


def offer_key(q):return q['fixture'],q['bookmaker'],tuple(q['spec']),q['sourceReceipt'],q['odd']


def score(rows,fits):
    groups=defaultdict(list)
    for r in rows:
        f=fits[r['metric']]
        if not f['point']['fitted']:continue
        raw=r['statWinOver'];push=r['modelPush'];market=r['marketOver'];new=predict(r['statOver'],market,f['point']['theta']);target=np.array([r['yOver'],r['actualPush'],1-r['yOver']-r['actualPush']]);v=[]
        for win in (raw,market*(1-push),new*(1-push)):
            a=np.array([win,push,1-win-push]);v.append([float(-(target*np.log(np.clip(a,1e-12,1))).sum()),float(((a-target)**2).sum())])
        groups[r['fixture']].append(v)
    if not groups:return dict(events=0,fixtures=0)
    a=np.asarray([np.mean(groups[k],axis=0) for k in sorted(groups)]);rng=np.random.default_rng(SEED);result=dict(events=sum(map(len,groups.values())),fixtures=len(groups),**{k:dict(logLoss=float(a[:,i,0].mean()),brier=float(a[:,i,1].mean())) for i,k in enumerate(KINDS)})
    for i,k in [(0,'stat'),(1,'market')]:
        delta=a[:,2,0]-a[:,i,0];sample=[float(np.mean(rng.choice(delta,size=len(delta)))) for _ in range(2000)];result['residualMinus'+k.title()]=dict(logLoss=float(delta.mean()),fixture95=np.quantile(sample,[.025,.975]).tolist())
    return result


def main(out):
    binding=read(out/'input_bindings.json');bad=[p for p,h in binding['files'].items() if not (ROOT/p).exists() or digest((ROOT/p).read_bytes())!=h]
    if bad:raise ValueError('Changed market residual inputs '+str(bad))
    signature=digest((out/'input_bindings.json').read_bytes());history=read(PRIOR/'history.json.gz');truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};results={};allbanks={};first={};coverage={}
    for packet in ('earliest','latest'):
        q=read(SOURCE/f'quotes_{packet}.json.gz');saved=read(SOURCE/f'replay_checkpoint_{packet}.json.gz');restored,parity=restore(q,saved['proper'],truth,saved['options']);refs,refaudit=references(q);lookup={offer_key(row):refs[i] for i,row in enumerate(q) if i in refs};frame=[]
        for r in restored:
            ref=refs.get(r['quoteIndex'])
            if not ref:continue
            quote=r['quote'];market=ref['q'] if quote['spec'][2]=='OVER' else 1-ref['q'];win,push=r['probabilities']['coupled']
            frame.append(dict(quoteIndex=r['quoteIndex'],fixture=r['fixture'],metric=r['metric'],fold=r['fold'],kickoff=quote['kickoff'],featureCutoff=quote['featureCutoff'],statOver=win/(1-push),statWinOver=win,modelPush=push,marketOver=market,yOver=r['y'],actualPush=r['push'],independent=ref['independent'],referenceBooks=ref['books']))
        training=[r for r in frame if not r['actualPush']];fits={}
        for fold in (*FOLDS[1:],'2026-10-19'):
            cutoff=absolute_time(fold+'T00:00:00Z');fits[fold]={m:bank(training,m,cutoff) for m in COUNT_METRICS};print('RESIDUAL_FIT',packet,fold,{m:{k:v['point'][k] for k in ('fixtures','events','fitted','theta')} for m,v in fits[fold].items()},flush=True)
        options=defaultdict(list)
        for row in saved['options']['coupled']:
            valid=[d for d in FOLDS[1:] if absolute_time(d+'T00:00:00Z')<=row['featureCutoff']];reference=lookup.get(offer_key(row))
            if not valid or reference is None:continue
            fold=max(valid);parameters=fits[fold][row['spec'][0]]
            if not parameters['point']['fitted']:continue
            options['stat'].append(row)
            for kind in ('market','residual'):
                c=candidate(row,reference,parameters,kind)
                if c is not None:options[kind].append(c)
        reports={};selected={}
        for kind in KINDS:
            for mode,picks in arms(options[kind]).items():
                name=kind+'_'+mode;selected[name]=picks;graded,missing=settle_selected(picks,truth);s=canonical_summary(graded);low=s.get('profitUnits',0)-sum(missing.values());known={offer_key(r) for r in graded};unknown=[r for r in picks if offer_key(r) not in known];high=low+sum(r['odd'] for r in unknown)
                reports[name]=dict(overall=s,selectedBeforeSettlement=len(picks),missingActual=missing,allSelectedProfitBounds=[low,high],allSelectedRoiBounds=[low/len(picks),high/len(picks)] if picks else None,byMetric={m:canonical_summary([r for r in graded if r['spec'][0]==m]) for m in COUNT_METRICS},picks=graded)
        scores={fold:{'overall':score([r for r in frame if r['fold']==fold],fits[fold]),**{m:score([r for r in frame if r['fold']==fold and r['metric']==m],fits[fold]) for m in COUNT_METRICS}} for fold in FOLDS[1:]}
        coverage[packet]=dict(parity=parity,referenceAudit=refaudit,properWithReference=len(frame),properWithoutReference=len(restored)-len(frame),independentProperEvents=sum(r['independent'] for r in frame),sameBookProperEvents=sum(not r['independent'] for r in frame),referenceFixtures={m:len({r['fixture'] for r in frame if r['metric']==m}) for m in COUNT_METRICS})
        write(out/f'forecast_frame_{packet}.json.gz',frame,zipped=True);write(out/f'priced_{packet}.json.gz',dict(reports=reports,selected=selected,properScores=scores),zipped=True);allbanks[packet]=fits;results[packet]=dict(reports={k:{a:b for a,b in r.items() if a!='picks'} for k,r in reports.items()},properScores=scores)
        if packet=='earliest':first=selected
        write(out/'progress.json',dict(stage='EVALUATE',packet=packet,providerCalls=0,llmCalls=0));print('RESIDUAL_RESULT',packet,{k:(r['overall']['bets'],r['overall'].get('roi')) for k,r in reports.items()},flush=True)
    latest=read(SOURCE/'quotes_latest.json.gz');price={k:prices(v,latest) for k,v in first.items()};write(out/'fixed_first_prices.json.gz',price,zipped=True);write(out/'parameter_banks.json',dict(fits=allbanks,inputSignature=signature,deployed=False))
    summary=dict(status='COMPLETE',primary='residual_conservative',results=results,coverage=coverage,priceAudit={k:{a:b for a,b in v.items() if a!='rows'} for k,v in price.items()},providerCalls=0,llmCalls=0,baseRateRefits=0,parameterFitCalls=312,successfulFits=sum(int(b['point']['fitted'])+sum(int(f['fitted']) for f in b['replicas']) for packet in allbanks.values() for fold in packet.values() for b in fold.values()),roiOptimized=False,thresholdsChanged=False,untouchedHoldout=False,deployed=False,inputSignature=signature)
    write(out/'summary.json',summary);write(out/'progress.json',dict(stage='COMPLETE',status='COMPLETE',providerCalls=0,llmCalls=0))
    lines=['# Πρόβλεψη πλεονεκτήματος έναντι της αγοράς','','Σταθερά prior-only μοντέλα ανά metric, ίδιοι αντίπαλοι και ίδιοι περιορισμοί. Ανάπτυξη, όχι ανεξάρτητο holdout.','', '| Σκέλος | Πρώτη: bets / ROI | Τελευταία: bets / ROI |','|---|---:|---:|']
    for kind in KINDS:
        for mode in ('point','conservative'):
            name=kind+'_'+mode;cells=[]
            for packet in ('earliest','latest'):
                r=results[packet]['reports'][name]['overall'];cells.append(str(r['bets'])+' / '+(f"{r['roi']:+.2%}" if 'roi' in r else '—'))
            lines.append('| '+name+' | '+' | '.join(cells)+' |')
    lines+=['','Το market benchmark χρησιμοποιεί την ίδια model push mass· δεν ισχυρίζεται ότι η αγορά δίνει γνωστή πιθανότητα push. Οι decisions κρατούν όλους τους αρχικούς στατιστικούς/favourite περιορισμούς και μόνο μειώνουν την raw πιθανότητα. Πρώτα point winner, μετά conservative abstention χωρίς replacement. Κανένα ROI tuning ή αυτόματη προαγωγή.']
    (out/'RESULTS_GR.md').write_text('\n'.join(lines)+'\n')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
