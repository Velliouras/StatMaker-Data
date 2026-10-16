"""Diagnose selection failures and test a prior-fitted market information layer.

Frozen statistical cores; no new count fits, provider requests or ROI tuning.
Existing 60% and odds limits remain fixed. A separately named no-floor arm is
diagnostic only and never changes app policy. Inspected periods are development.
"""
import argparse
from collections import Counter,defaultdict
import gzip
import json
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.market_calibration import references,fit_weight,pooled_probability,select_ev,partition
from betting_v2.live_revised import load_revised
from betting_v2.reliable_counts import metric_values
from betting_v2.validated_walk_forward import distribution,FOLDS
from betting_v2.research_quotes import assess,stats
from betting_v2.research_scope_expansion import select_scope
from betting_v2.research_app_markets import prepare_extended,outcome_extended,PERIOD_SCOPES
from betting_v2.exact_forecast_adapter import forecast

ROOT=Path(__file__).resolve().parents[2]
ARMS=('stat_favourite','stat_ev_same_gates','stat_ev_no_direction_gate',
      'stat_ev_reference_only','market_ev_60','market_reference_ev_60',
      'market_ev_positive_only_DIAGNOSTIC')


def preference(spec,fav):
    if not fav:return 1
    metric,scope,side,team,_=spec
    if team:return 0 if (team==fav and side=='OVER') or (team!=fav and side=='UNDER') else 2
    if scope in ('RESULT','DC','DNB'):
        return 0 if side in (fav,'HOME_OR_DRAW' if fav=='HOME' else 'AWAY_OR_DRAW','HOME_OR_AWAY') else 2
    if scope=='BTTS':return 0 if side=='NO' else 2
    return 1


def fixture_favourite(quotes):
    prices=defaultdict(dict)
    for q in quotes:
        if tuple(q['spec'][:2])==('GOALS_FT','RESULT'):prices[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
    result={}
    for key,p in prices.items():
        if set(p)=={'HOME','DRAW','AWAY'}:
            side=min(p,key=p.get)
            if side!='DRAW' and p[side]<=1.8:result[key]=side
    return result


def frame_row(q,row,profile,prepared,full,actual,first,fav):
    point=forecast(row,profile,q['spec'],prepared)
    eligible=assess(q,(row,prepared,full),profile,fav,'current_preference')
    y,push=outcome_extended(*actual,q['spec'],first=first)
    p=point['winProbability'];model_push=point['pushProbability']
    return dict(q,p=p,push=model_push,ev=p*q['odd']+model_push-1,y=y,actualPush=push,
        currentEligible=eligible is not None,confidence=eligible['confidence'] if eligible else p,
        preference=preference(q['spec'],fav),tier=eligible['tier'] if eligible else 'UNSELECTED',fav=fav)


def scope_frame(args,history,packet):
    path=ROOT/'reports/betting_v2/scope_expansion_v2_20261016'/('quotes_'+packet+'.json.gz')
    quotes=read(path);qsha=digest(path.read_bytes())
    history_sha=digest(json.dumps(history,sort_keys=True,separators=(',',':')).encode())
    source_sha=digest(Path(__file__).read_bytes())
    signature=digest((qsha+history_sha+source_sha+digest((ROOT/'reports/betting_v2/all_market_revision_20261016/deployment.json').read_bytes())).encode())
    target=args.output/('frame_'+packet+'.json.gz')
    if target.exists():
        saved=read(target)
        if saved['signature']==signature:return saved
        raise ValueError('Changed cached frame signature; use a new output directory')
    truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};favours=fixture_favourite(quotes)
    frame=[];missing=Counter()
    for cutoff in sorted({q['featureCutoff'] for q in quotes}):
        model,profile,_=load_revised(ROOT,history,cutoff);cache={}
        for q in quotes:
            if q['featureCutoff']!=cutoff:continue
            raw=truth.get(q['fixture']);metric=q['spec'][0]
            if raw is None:missing['noActualFixture']+=1;continue
            actual=metric_values(raw,metric);first=metric_values(raw,'GOALS_1H')
            if actual is None:missing['noActual::'+metric]+=1;continue
            if q['spec'][1] in PERIOD_SCOPES and first is None:missing['noPeriodActual']+=1;continue
            key=q['_home'],q['_away'],metric
            if key not in cache:
                h,a=q['_home'],q['_away'];pred=model.predict(h,a,metric);full=model.predict(h,a,'GOALS_FT')
                if pred is None or full is None:cache[key]=None
                else:
                    regime='XG' if min(len(model.xg[h]),len(model.xg[a]))>=8 else 'ELO_NO_XG'
                    row=dict(metric=metric,regime=regime,means=list(pred[:2]))
                    if metric in ('GOALS_1H','GOALS_2H'):row.update(fullMeans=list(full[:2]),shares=[pred[i]/full[i] for i in (0,1)])
                    if metric=='GOALS_FT':
                        half=model.predict(h,a,'GOALS_1H')
                        if half is not None:row['periodShares']=[half[i]/full[i] for i in (0,1)]
                    cache[key]=row,prepare_extended(row,profile),distribution(dict(metric='GOALS_FT',regime=regime,means=list(full[:2])),profile)
            pred=cache[key]
            if pred is None:missing['noPrediction::'+metric]+=1;continue
            if q['spec'][1] in PERIOD_SCOPES and 'periodShares' not in pred[0]:continue
            fav=favours.get((q['fixture'],q['bookmaker'],q['sourceReceipt']))
            frame.append(frame_row(q,pred[0],profile,pred[1],pred[2],actual,first,fav))
        print('MARKET_FRAME',packet,cutoff,len(frame),flush=True)
    result=dict(signature=signature,quoteSha256=qsha,historySha256=history_sha,rows=frame,missing=dict(missing))
    write(target,result,zipped=True);return result


def csv_frame():
    csvroot=ROOT/'reports/betting_v2/csv_prices_v2_20261016'
    frozen=read(csvroot/'frozen_predictions.json.gz');joined=read(csvroot/'joined_quotes.json.gz')
    revision=ROOT/'reports/betting_v2/model_revision_20261016';calroot=ROOT/'reports/betting_v2/model_revision_calibration_20261016'
    catalogue={r['fixture']:r for fold in frozen['folds'] for r in fold['fixtures']};folds=[]
    def make(rows,packets,profile):
        lookup={r['fixture']:r for r in rows};quotes=[]
        for packet in packets:
            source=lookup.get(packet['fixture'])
            if source is None:continue
            for q in packet['quotes']:
                if q['bookmaker']!='Bet365' or q['priceType']!='closing':continue
                quotes.append(dict(q,fixture=source['fixture'],kickoff=source['date'],featureCutoff=source['featureCutoff'],
                    sourceReceipt='csv:'+packet['sourceSha256']+':'+str(packet['csvRow']),sourceKind='CSV_CLOSING_BENCHMARK'))
        favourites=fixture_favourite(quotes);frame=[];cache={}
        for q in quotes:
            r=lookup[q['fixture']]
            if r['fixture'] not in cache:
                prepared=prepare_extended(r,profile);cache[r['fixture']]=prepared,distribution(r,profile)
            prepared,full=cache[r['fixture']];fav=favourites.get((q['fixture'],q['bookmaker'],q['sourceReceipt']))
            frame.append(frame_row(q,r,profile,prepared,full,r['actual'],None,fav))
        return frame
    # The July seed uses raw predictions from a core fitted before July. Do not
    # apply a profile learned from those same July outcome labels to this seed.
    seedrows=read(revision/'recency_2026-08-01_calibration.json.gz')
    seed=make(seedrows,read(revision/'recency_2026-08-01_prior_price_joins.json.gz'),{})
    for fitday,day,endday in FOLDS:
        rows=read(revision/('recency_'+day+'_predictions.json.gz'))
        profile=read(revision/('recency_'+day+'_profile.json'))
        cal=read(calroot/('recency_'+day+'_selected_calibration.json'));profile['_selected_joint']=cal['point']['theta']
        ids={f['fixture'] for fold in frozen['folds'] if fold['testFrom']==day for f in fold['fixtures']}
        frame=make(rows,[p for p in joined if p['fixture'] in ids],profile)
        folds.append(dict(day=day,rows=frame))
    return seed,folds


def attach_reference(rows):
    refs,audit=references(rows)
    return [dict(r,reference=refs.get(i)) for i,r in enumerate(rows)],audit


def training_records(rows,metric,cutoff):
    return [dict(fixture=r['fixture'],kickoff=r['kickoff'],statConditional=r['p']/(1-r['push']),
        marketConditional=r['reference']['q'],y=r['y']) for r in rows
        if r['reference'] and r['spec'][0]==metric and partition(r['spec']) is not None
        and not r['actualPush'] and r['push']<1 and r['kickoff']<cutoff]


def candidates(rows,fit_for):
    options={a:[] for a in ARMS};score=defaultdict(list)
    for r in rows:
        metric=r['spec'][0];ref=r['reference'];pool=None
        if ref:
            weight=fit_for(r)['weight'];pool=pooled_probability(r['p'],r['push'],ref['q'],weight)
            if not r['actualPush']:
                score[metric].append(dict(fixture=r['fixture'],stat=(r['p']/(1-r['push'])-r['y'])**2,
                    market=(ref['q']-r['y'])**2,pool=(pool/(1-r['push'])-r['y'])**2))
        if not 1.8<r['odd']<3:continue
        if r['currentEligible']:
            options['stat_favourite'].append(r);options['stat_ev_same_gates'].append(r)
        if r['p']>=.6 and r['ev']>0:
            options['stat_ev_no_direction_gate'].append(r)
            if ref:options['stat_ev_reference_only'].append(r)
        if ref:
            market=ref['q']*(1-r['push'])
            for arm,p,floor in [('market_ev_60',pool,.6),('market_reference_ev_60',market,.6),('market_ev_positive_only_DIAGNOSTIC',pool,0.)]:
                ev=p*r['odd']+r['push']-1
                if p>=floor and ev>1e-12:
                    options[arm].append(dict(r,p=p,ev=ev,confidence=p,pointStatProbability=r['p'],marketProbability=market,
                        tier='HIGH' if p>=.8 else 'CONFIRMED' if p>=.7 else 'DEVELOPING'))
    return options,score


def report(options):
    result={}
    for arm,rows in options.items():
        picks=select_scope(rows) if arm=='stat_favourite' else select_ev(rows)
        result[arm]=dict(overall=stats(picks),expectedProfitUnits=float(sum(r['p']*r['odd']+r['push']-1 for r in picks)),
            byMetric={m:stats([p for p in picks if p['spec'][0]==m]) for m in sorted({p['spec'][0] for p in picks})},picks=picks)
    return result


def score_report(scores):
    result={}
    for metric,rows in scores.items():
        byfixture=defaultdict(list)
        for r in rows:byfixture[r['fixture']].append(r)
        result[metric]=dict(fixtures=len(byfixture),events=len(rows),**{k:float(np.mean([np.mean([r[k] for r in v]) for v in byfixture.values()])) for k in ('stat','market','pool')})
    return result


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    write(args.output/'status.json',dict(status='STARTED',providerCalls=0,llmCalls=0,newCoreFits=0))
    seed,folds=csv_frame();seed,seedaudit=attach_reference(seed)
    write(args.output/'csv_seed.json.gz',seed,zipped=True)
    prior=list(seed);all_options={a:[] for a in ARMS};all_scores=defaultdict(list);fits={};csvframes=[]
    for fold in folds:
        rows,audit=attach_reference(fold['rows']);cutoff=direct.timestamp(fold['day'])
        fit=fit_weight(training_records(prior,'GOALS_FT',cutoff),cutoff);fits[fold['day']]=fit
        opts,scores=candidates(rows,lambda _:fit)
        for a,values in opts.items():all_options[a].extend(values)
        for m,values in scores.items():all_scores[m].extend(values)
        csvframes.extend(rows);prior.extend(rows)
    csvreport=dict(arms=report(all_options),probabilityScores=score_report(all_scores),fits=fits,seedReferenceAudit=seedaudit)
    write(args.output/'csv_frames.json.gz',csvframes,zipped=True);write(args.output/'csv_results.json',csvreport)
    print('MARKET_CSV',json.dumps({a:r['overall'] for a,r in csvreport['arms'].items()}),'FITS',fits,flush=True)
    history=direct.load_actual_results(direct.timestamp('2026-10-16'))['GLOBAL'];scope={}
    # Receipt study can use only CSV forecast outcomes completed before October.
    csvprior=[r for r in prior if r['kickoff']<direct.timestamp('2026-10-01')]
    for packet in ('earliest','latest'):
        frame=scope_frame(args,history,packet);rows,audit=attach_reference(frame['rows']);weight_cache={}
        def fit_for(r):
            metric=r['spec'][0];cutoff=r['featureCutoff'];key=metric,cutoff
            if key not in weight_cache:
                training=training_records(csvprior+rows,metric,cutoff)
                weight_cache[key]=fit_weight(training,cutoff)
            return weight_cache[key]
        options,scores=candidates(rows,fit_for);results=report(options)
        # Reproduce the immediately preceding frozen all-market point control.
        original=read(ROOT/'reports/betting_v2/all_market_revision_20261016'/('priced_'+packet+'.json'))['arms']['all_market_point']['overall']
        reproduced=results['stat_favourite']['overall']
        if original['bets']!=reproduced['bets'] or abs(original['profitUnits']-reproduced['profitUnits'])>1e-9:
            raise ValueError('Prior point control did not reproduce '+packet)
        calibration={m+'::'+str(int(t)):v for (m,t),v in weight_cache.items()}
        scope[packet]=dict(arms=results,probabilityScores=score_report(scores),referenceAudit=audit,calibration=calibration,
            sourceSignature=frame['signature'],missing=frame['missing'])
        write(args.output/('receipt_'+packet+'_results.json'),scope[packet])
        print('MARKET_RECEIPT',packet,json.dumps({a:r['overall'] for a,r in results.items()}),flush=True)
    summary=dict(status='COMPLETE',arms=list(ARMS),csv={k:v for k,v in csvreport.items() if k!='arms'},
        csvResults={a:{k:v for k,v in r.items() if k!='picks'} for a,r in csvreport['arms'].items()},
        receipts={p:{**{k:v for k,v in r.items() if k!='arms'},'results':{a:{k:v for k,v in v.items() if k!='picks'} for a,v in r['arms'].items()}} for p,r in scope.items()},
        providerCalls=0,llmCalls=0,newCoreFits=0,appChanges=0,roiOptimized=False,untouchedHoldout=False,
        noFloorArm='Diagnostic only; app 60% probability requirement is unchanged',
        limitations=['Previously inspected development periods, not untouched validation',
            'CSV closing prices have no exact observation timestamps and do not prove executable profit',
            'Same-book reference is not independent market confirmation; explicitly labelled',
            'July seed uses raw prior-trained statistical forecasts; later frames use frozen calibrated forecasts',
            'Market pooling is a selection-calibration experiment, not a coherent replacement of every joint forecast',
            'No winner is selected from test ROI and no new app deployment occurs'])
    write(args.output/'summary.json',summary);write(args.output/'status.json',dict(status='COMPLETE',providerCalls=0,llmCalls=0,newCoreFits=0))

if __name__=='__main__':main()
