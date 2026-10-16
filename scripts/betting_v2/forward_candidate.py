"""Freeze unseen future fixtures and score prior immutable candidate cohorts.

Runs on cached receipts/results only. No training, threshold tuning, provider
requests or LLM calls. Each fixture is frozen once across daily cohorts.
"""
import argparse
from collections import Counter,defaultdict
import copy
from datetime import datetime,timezone
import gzip,json,math,sqlite3,tempfile,zipfile
import fcntl
import subprocess
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.research_scope_expansion import expanded_spec,select_scope
from betting_v2.index_quote_receipts import extracted_offers
from betting_v2.research_market_decisions import attach_reference
from betting_v2.market_calibration import pooled_probability
from betting_v2.live_revised import load_revised
from betting_v2.research_app_markets import prepare_extended,PERIOD_SCOPES
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.validated_walk_forward import distribution
from betting_v2.research_quotes import assess,feature_cutoff,one_fixture_receipt,stats
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_model_revision import read
from betting_v2.score_forward_research import settled,paired_comparisons

ROOT=Path(__file__).resolve().parents[2]


def future_quotes(history,now,known):
    manifest=read(ROOT/'data/statmaker/app_ready/update_manifest.json');bundle=next(a for a in manifest['artifacts'] if a['id']=='app_ready_betting_bundle')
    teams={r[k] for r in history for k in ('_home','_away')};catalog=defaultdict(list);counters=Counter()
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(ROOT/bundle['path']) as archive:
            archive.extract('databases/statmaker_prepared_betting.db',td)
        with sqlite3.connect(Path(td)/'databases/statmaker_prepared_betting.db') as con:
            con.row_factory=sqlite3.Row
            for raw in con.execute('SELECT * FROM prepared_fixture_matches WHERE usable_for_stats=1'):
                f=dict(raw);ko=direct.kickoff(f['kickoff']);event=str(f['id'])+'|'+str(ko)
                if not now<ko<now+36*3600 or event in known:continue
                aliases=direct.UEFA_ALIASES.get(direct.league_code(f['league_code']),{})
                def identity(keys):
                    values=[aliases.get(direct.name(f[k]),direct.name(f[k])) for k in keys]
                    return next((v for v in values if v in teams),None)
                h=identity(('canonical_home_team','home_team','provider_home_team'));a=identity(('canonical_away_team','away_team','provider_away_team'))
                if h and a:catalog[str(f['id']),ko].append(dict(f,_home=h,_away=a))
    packets=defaultdict(dict)
    for path in sorted((ROOT/'reports/betting_v2/quote_receipts').glob('*.gz')):
        try:
            raw=path.read_bytes();doc=json.loads(gzip.decompress(raw));offers,_=extracted_offers(doc)
        except (ValueError,KeyError,TypeError,OSError,EOFError):continue
        events={str(v['event'].get('id') or v['event'].get('eventId')):v['event'] for v in doc['boundedMarketSnapshot']}
        for offer in offers:
            ko=direct.kickoff(offer['kickoffUTC']);obs=direct.kickoff(offer['clientReceivedAtUTC'])
            if not now-86400<=obs<=now<ko:continue
            choices=catalog.get((str(offer['providerEventId']),ko),[])
            pairs={(r['_home'],r['_away']) for r in choices}
            if len(pairs)!=1:continue
            spec=expanded_spec(offer,events[offer['providerEventId']])
            if spec is None or 'CARD' in spec[0]:counters['unsupportedContract']+=1;continue
            h,a=next(iter(pairs));fixture=f'{ko}:{h}:{a}';q=dict(offer,fixture=fixture,kickoff=ko,observed=obs,
                featureCutoff=feature_cutoff(obs),spec=spec,sourceReceipt=path.name,sourceSha256=digest(raw),_home=h,_away=a,
                eventIdentity=str(offer['providerEventId'])+'|'+str(ko),sourceMarket=offer['providerMarketName'])
            key=fixture,path.name,q['bookmaker'];identity=tuple(spec);previous=packets[key].get(identity)
            if previous and (previous.get('ambiguous') or previous['odd']!=q['odd']):q['ambiguous']=True
            packets[key][identity]=q
    quotes=one_fixture_receipt([q for p in packets.values() for q in p.values() if not q.get('ambiguous')],latest=True)
    return quotes,bundle,dict(counters)


def freeze(out,index):
    now=datetime.now(timezone.utc).timestamp();cutoff=math.floor(now/86400)*86400
    history=direct.load_actual_results(cutoff)['GLOBAL'];known=set(index.get('events',[]));quotes,bundle,counters=future_quotes(history,now,known)
    if not quotes:return None
    source=ROOT/'reports/betting_v2/combined_candidate_20261016'
    banks={m:read(p) for p in source.glob('*_profiles.json') for m in [p.name.split('_profiles')[0]]}
    fits=read(ROOT/'reports/betting_v2/market_decision_revision_20261016/receipt_latest_results.json')['calibration']
    references,_=attach_reference(quotes);reference={id(q):r['reference'] for q,r in zip(quotes,references)}
    triplets=defaultdict(dict)
    for q in quotes:
        if tuple(q['spec'][:2])==('GOALS_FT','RESULT'):triplets[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
    models={name:dict(forecasts=[],selected=[]) for name in ('control','coupled_market_point','coupled_market_conservative')};options={k:[] for k in models}
    for feature_day in sorted({q['featureCutoff'] for q in quotes}):
        control,old,_=load_revised(ROOT,history,feature_day);candidate=copy.copy(control)
        candidate.cache={};candidate.uncertainty_cache={};candidate.count_profiles=banks;profile=dict(old)
        for p in banks.values():profile.update(p[0])
        candidate.revision_profile=profile;cache={}
        for q in quotes:
            if q['featureCutoff']!=feature_day:continue
            h,a=q['_home'],q['_away'];metric=q['spec'][0];triplet=triplets[q['fixture'],q['bookmaker'],q['sourceReceipt']];fav=None
            if set(triplet)=={'HOME','DRAW','AWAY'}:
                side=min(triplet,key=triplet.get)
                if side!='DRAW' and triplet[side]<=1.8:fav=side
            for name,model,p in [('control',control,old),('coupled_market_point',candidate,profile)]:
                key=name,h,a,metric
                if key not in cache:
                    rate=model.predict(h,a,metric);full=model.predict(h,a,'GOALS_FT')
                    if rate is None or full is None:cache[key]=None
                    else:
                        regime='XG' if min(len(model.xg[h]),len(model.xg[a]))>=8 else 'ELO_NO_XG'
                        row=dict(metric=metric,regime=regime,means=list(rate[:2]))
                        if metric in ('GOALS_1H','GOALS_2H'):row.update(fullMeans=list(full[:2]),shares=[rate[i]/full[i] for i in (0,1)])
                        if metric=='GOALS_FT':
                            half=model.predict(h,a,'GOALS_1H')
                            if half:row['periodShares']=[half[i]/full[i] for i in (0,1)]
                        cache[key]=row,prepare_extended(row,p),distribution(dict(metric='GOALS_FT',regime=regime,means=list(full[:2])),old)
                pred=cache[key]
                if pred is None or q['spec'][1] in PERIOD_SCOPES and 'periodShares' not in pred[0]:continue
                point=forecast(pred[0],p,q['spec'],pred[1]);win=point['winProbability'];push=point['pushProbability']
                if name=='coupled_market_point' and metric.startswith('GOALS'):
                    ref=reference[id(q)];eligible_fits=[v for k,v in fits.items() if k.startswith(metric+'::') and v['trainingBefore']<=feature_day]
                    if ref is None or not eligible_fits:continue
                    fit=max(eligible_fits,key=lambda v:v['trainingBefore']);win=min(win,pooled_probability(win,push,ref['q'],fit['weight']))
                record=dict(q,winProbability=win,pushProbability=push,homeMean=point['homeMean'],awayMean=point['awayMean'],status='UNSCORED')
                models[name]['forecasts'].append(record)
                if name=='coupled_market_point':models['coupled_market_conservative']['forecasts'].append(record)
                pick=assess(q,pred,p,fav,'current_preference')
                if pick is None or win<.6 or win*q['odd']+push-1<=0:continue
                pick=dict(pick,p=win,push=push,ev=win*q['odd']+push-1,confidence=min(pick['confidence'],win))
                if name=='coupled_market_point':options[name].append(pick)
                if metric.startswith('GOALS') and q['spec'][1] not in ('RESULT','DC','DNB','TOTAL','TEAM','BTTS'):continue
                bound=model.bounds(h,a,q['spec'],q['odd'])
                if bound is None:continue
                lower=min(win,bound['win']);ev=lower*q['odd']+bound['push']-1;support=bound[fav.lower()] if fav else None
                if lower<.6 or ev<=0:continue
                if (metric.startswith('GOALS') or metric in ('CORNERS','SHOTS','SOT')) and support is not None and support<.6:continue
                confidence=min(lower,support) if support is not None else lower
                options['control' if name=='control' else 'coupled_market_conservative'].append(dict(pick,p=lower,push=bound['push'],ev=ev,confidence=confidence,
                    tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING'))
    finished=datetime.now(timezone.utc).timestamp()
    for name,data in models.items():
        data['forecasts']=[r for r in data['forecasts'] if r['kickoff']>finished+60]
        data['selected']=select_scope([r for r in options[name] if r['kickoff']>finished+60])
    stamp=datetime.fromtimestamp(finished,timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');target=out/('frozen_'+stamp+'.json.gz')
    if target.exists():raise ValueError('Refusing to replace an immutable cohort')
    doc=dict(schema=1,frozenAtUTC=datetime.fromtimestamp(finished,timezone.utc).isoformat(),models=models,bundle=bundle,
        futureOutcomesRead=False,providerCalls=0,llmCalls=0,coreRefits=0,publicationTimeOfHistoricalInputsVerified=False,
        historySha256=digest(json.dumps(history,sort_keys=True,separators=(',',':')).encode()),
        profileSha256={p.name:digest(p.read_bytes()) for p in source.glob('*_profiles.json')},
        goalCalibrationSha256=digest((ROOT/'reports/betting_v2/market_decision_revision_20261016/receipt_latest_results.json').read_bytes()),
        rules=dict(prices='Latest single prematch receipt per fixture, <=24h old; exact provider ID and kickoff',
            selection='Unchanged odds/p60/favourite/one-family rules; point and conservative candidates remain separate',
            featureLag='At least one complete UTC day before receipt; frozen coefficient/profile banks',
            overlap='Each provider event and kickoff frozen once across cohorts',executionEvidence=False),counters=counters)
    write(target,doc,zipped=True)
    events={r['eventIdentity'] for data in models.values() for r in data['forecasts']}
    index.setdefault('cohorts',[]).append(dict(path=target.name,sha256=digest(target.read_bytes()),frozenAtUTC=doc['frozenAtUTC'],
        models={k:dict(forecasts=len(v['forecasts']),fixtures=len({r['fixture'] for r in v['forecasts']}),selected=len(v['selected'])) for k,v in models.items()}))
    index['events']=sorted(known|events);return target.name


def score(out,index):
    actual_rows=direct.load_actual_results(datetime.now(timezone.utc).timestamp())['GLOBAL']
    actual={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in actual_rows}
    proof_path=out/'publication_evidence.json';proof=read(proof_path) if proof_path.exists() else {}
    for item in index.get('cohorts',[]):
        path=out/item['path']
        if digest(path.read_bytes())!=item['sha256']:raise ValueError('Immutable cohort hash changed')
        doc=read(path);when=datetime.fromisoformat(doc['frozenAtUTC']).timestamp();result={};events={}
        public=proof.get(item['path']);verified=datetime.fromisoformat(public['publiclyVerifiedAtUTC']).timestamp() if public else None
        if public and public['sha256']!=item['sha256']:raise ValueError('Publication proof hash mismatch')
        for name,data in doc['models'].items():
            eligible_picks=public_rows(data['selected'],verified);eligible_forecasts=public_rows(data['forecasts'],verified)
            picks,missing=settled(eligible_picks,actual,when);rows,missing_rows=settled(eligible_forecasts,actual,when)
            result[name]=dict(selected=stats(picks),selectedUnscored=missing if public else len(data['selected']),forecastScored=len(rows),
                forecastUnscored=missing_rows if public else len(data['forecasts']),
                eligibleSelected=len(eligible_picks),eligibleFixtures=len({r['fixture'] for r in eligible_forecasts}),
                selectedExcludedLatePublication=len(data['selected'])-len(eligible_picks) if public else 0,
                fixturesExcludedLatePublication=len({r['fixture'] for r in data['forecasts'] if r['kickoff']<=verified}) if public else 0)
            events[name]={(r['fixture'],r['bookmaker'],tuple(r['spec'])):r for r in rows if not r['actualPush']}
        write(out/(path.stem.replace('.json','')+'_score.json'),dict(frozenSha256=item['sha256'],scoredAtUTC=datetime.now(timezone.utc).isoformat(),
            models=result,publicationVerified=bool(public),publicationEvidence=public,
            pairedComparisons=paired_comparisons(events,baseline='control'),providerCalls=0,llmCalls=0,refits=0))


def public_rows(rows,verified):
    return [r for r in rows if r['kickoff']>verified] if verified is not None else []


def record_publication(out,index):
    path=out/'publication_evidence.json';proof=read(path) if path.exists() else {}
    for item in index.get('cohorts',[]):
        if item['path'] in proof:continue
        file=out/item['path'];relative=str(file.resolve().relative_to(ROOT))
        subprocess.run(['git','ls-files','--error-unmatch',relative],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
        commit=subprocess.check_output(['git','log','-1','--format=%H','--',relative],cwd=ROOT,text=True).strip()
        if not commit:raise ValueError('No committed snapshot for publication proof')
        if digest(file.read_bytes())!=item['sha256']:raise ValueError('Publication source changed')
        proof[item['path']]=dict(commit=commit,sha256=item['sha256'],publiclyVerifiedAtUTC=datetime.now(timezone.utc).isoformat(),
            method='Recorded after successful Git push; later-than-verification kickoffs only')
    write(path,proof)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--record-publication',action='store_true');args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    lockpath=Path(tempfile.gettempdir())/('statmaker-forward-'+digest(str(args.output.resolve()).encode())[:16]+'.lock')
    with lockpath.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=args.output/'index.json';index=read(path) if path.exists() else dict(schema=1,cohorts=[],events=[])
        if args.record_publication:
            record_publication(args.output,index);return
        binding=read(ROOT/'reports/betting_v2/combined_candidate_20261016/source_binding.json')
        changed=[p for p,sha in binding['files'].items() if digest((ROOT/p).read_bytes())!=sha]
        created=None if changed else freeze(args.output,index)
        write(path,index);score(args.output,index)
        write(args.output/'status.json',dict(status='COMPLETE',newCohort=created,cohorts=len(index['cohorts']),providerCalls=0,llmCalls=0,refits=0,
            freezeSkippedForChangedModelInputs=changed))
        print('FORWARD_CANDIDATE',created,index['cohorts'][-1] if index['cohorts'] else {},flush=True)

if __name__=='__main__':main()
