"""Prospective first-receipt-per-count-market trial; cached data only."""
import argparse,fcntl,gzip,json,math,sqlite3,tempfile,zipfile
from collections import defaultdict,Counter
from datetime import datetime,timezone
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.forward_candidate import record_publication,public_rows
from betting_v2.forward_context import attach_market_times
from betting_v2.index_quote_receipts import extracted_offers
from betting_v2.research_scope_expansion import expanded_spec,select_scope
from betting_v2.research_quotes import feature_cutoff
from betting_v2.research_probability_recalibration import ROOT,PRIOR,FOLDS,MonthlyModels
from betting_v2.research_git_archive import COUNT_METRICS,absolute_time
from betting_v2.research_market_first_counts import retain
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.research_context_market import eligible,conservative_winner
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.score_forward_research import settled,paired_comparisons
from betting_v2.audit_probability_trial import canonical_summary

CONFIG=ROOT/'reports/betting_v2/forward_market_first_20261018/candidate.json'
KINDS=('control','coupled')


def first_markets(rows,known):
    chosen={};favs=fixture_favourite(rows);evidence=defaultdict(dict)
    for q in rows:
        if tuple(q['spec'][:2])==('GOALS_FT','RESULT'):
            evidence[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
    packets=defaultdict(list)
    for q in rows:
        key=q['fixture'],q['spec'][0]
        if key in known or q['spec'][0] not in COUNT_METRICS or q['spec'][1] not in ('TOTAL','TEAM'):continue
        fkey=q['fixture'],q['bookmaker'],q['sourceReceipt']
        packets[q['sourceReceipt']].append(dict(q,_favourite=favs.get(fkey),_sameReceiptResultOdds=evidence[fkey]))
    for packet in packets.values():retain(chosen,packet)
    return [q for v in chosen.values() for q in v['rows']]


def collect(history,now,known,until):
    manifest=read(ROOT/'data/statmaker/app_ready/update_manifest.json')
    bundle=next(a for a in manifest['artifacts'] if a['id']=='app_ready_betting_bundle')
    teams={r[k] for r in history for k in ('_home','_away')};catalog=defaultdict(set);counters=Counter()
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(ROOT/bundle['path']) as archive:archive.extract('databases/statmaker_prepared_betting.db',td)
        with sqlite3.connect(Path(td)/'databases/statmaker_prepared_betting.db') as con:
            con.row_factory=sqlite3.Row
            for raw in con.execute('SELECT * FROM prepared_fixture_matches WHERE usable_for_stats=1'):
                f=dict(raw);ko=direct.kickoff(f['kickoff'])
                if not now+60<ko<until:continue
                aliases=direct.UEFA_ALIASES.get(direct.league_code(f['league_code']),{})
                def identity(keys):
                    values=[aliases.get(direct.name(f[k]),direct.name(f[k])) for k in keys]
                    return next((v for v in values if v in teams),None)
                h=identity(('canonical_home_team','home_team','provider_home_team'));a=identity(('canonical_away_team','away_team','provider_away_team'))
                if h and a:catalog[str(f['id']),ko].add((h,a))
    rows=[]
    for path in sorted((ROOT/'reports/betting_v2/quote_receipts').glob('*.gz')):
        try:raw=path.read_bytes();doc=json.loads(gzip.decompress(raw));offers,_=extracted_offers(doc)
        except (ValueError,KeyError,TypeError,OSError,EOFError):counters['invalidReceipt']+=1;continue
        events={str(v['event'].get('id') or v['event'].get('eventId')):v['event'] for v in doc['boundedMarketSnapshot']};packet={}
        for q in offers:
            ko=direct.kickoff(q['kickoffUTC']);obs=direct.kickoff(q['clientReceivedAtUTC']);eid=str(q['providerEventId'])
            if not eid.isdigit() or not now-86400<=obs<=now or not now+60<ko<until:continue
            choices=catalog.get((eid,ko),set())
            if len(choices)!=1:counters['unboundOrAmbiguousIdentity']+=1;continue
            spec=expanded_spec(q,events[eid])
            if spec is None or not (spec[0] in COUNT_METRICS and spec[1] in ('TOTAL','TEAM') or tuple(spec[:2])==('GOALS_FT','RESULT')):continue
            h,a=next(iter(choices));fixture=f'{ko}:{h}:{a}'
            if (fixture,spec[0]) in known:continue
            value=dict(q,fixture=fixture,kickoff=ko,observed=obs,featureCutoff=feature_cutoff(obs),spec=spec,sourceReceipt=path.name,
                sourceSha256=digest(raw),_home=h,_away=a,eventIdentity=eid+'|'+str(ko),sourceMarket=q['providerMarketName'])
            key=fixture,q['bookmaker'],tuple(spec);previous=packet.get(key)
            if previous and (previous.get('ambiguous') or previous['odd']!=value['odd']):value['ambiguous']=True
            packet[key]=value
        rows.extend(q for q in packet.values() if not q.get('ambiguous'))
    rows,times=attach_market_times(rows,ROOT/'reports/betting_v2/quote_receipts')
    bundle=dict(bundle,actualSha256=digest((ROOT/bundle['path']).read_bytes()))
    return first_markets(rows,known),bundle,dict(counters=counters,timestamps=times)


def selected_arms(options):
    winners=select_scope(options)
    return dict(point=winners,conservative=[r for w in winners if (r:=conservative_winner(w)) is not None])


def freeze(out,index,config):
    now=datetime.now(timezone.utc).timestamp();until=absolute_time(config['freezeBeforeUTC'])
    if now>=until:return None,dict(expired=True)
    history=direct.load_actual_results(math.floor(now/86400)*86400)['GLOBAL']
    quotes,bundle,audit=collect(history,now,{tuple(k) for k in index.get('marketKeys',[])},until)
    if not quotes:return None,dict(audit,noEligibleQuotes=True)
    profiles={d:{m:read(PRIOR/f'{m}_{d}_coupled_profiles.json')['profiles'] if d!=FOLDS[-1]
        else read(ROOT/f'reports/betting_v2/combined_candidate_20261016/{m}_profiles.json') for m in COUNT_METRICS} for d in FOLDS}
    models=MonthlyModels(history,profiles);forecasts=defaultdict(list);options=defaultdict(list);diagnostics=Counter()
    for day in sorted({q['featureCutoff'] for q in quotes}):
        models.advance(day)
        for q in quotes:
            if q['featureCutoff']!=day:continue
            for kind in KINDS:
                pred=models.prediction(q['_home'],q['_away'],q['spec'][0],kind)
                if pred is None:diagnostics['missingPrediction::'+q['spec'][0]]+=1;continue
                v=forecast(pred['rows'][0],pred['profiles'][0],q['spec'],pred['prepared'][0]);fav=q['_favourite']
                forecasts[kind].append(dict(q,winProbability=v['winProbability'],pushProbability=v['pushProbability'],status='UNSCORED'))
                candidate=eligible(q,v,pred['full'],fav,v['winProbability'],v['pushProbability'])
                if candidate is None:continue
                replicas=[forecast(r,p,q['spec'],prep) for r,p,prep in zip(pred['rows'][1:],pred['profiles'][1:],pred['prepared'][1:])]
                support=None if not fav else min(v['scenarios'][fav]['winProbability'] or 0.,float(np.quantile([x['scenarios'][fav]['winProbability'] or 0. for x in replicas],.1)))
                candidate['_lower']=dict(p=min(v['winProbability'],float(np.quantile([x['winProbability'] for x in replicas],.1))),
                    push=min(v['pushProbability'],float(np.quantile([x['pushProbability'] for x in replicas],.1))),support=support)
                options[kind].append(candidate)
    finished=datetime.now(timezone.utc).timestamp();results={}
    for kind in KINDS:
        rows=[r for r in forecasts[kind] if r['kickoff']>finished+60]
        arms=selected_arms([r for r in options[kind] if r['kickoff']>finished+60])
        for suffix,picks in arms.items():results[kind+'_'+suffix]=dict(forecasts=rows,selected=picks)
    # Freeze even a no-prediction packet: do not retry a later price to obtain a pick.
    packets=[q for q in quotes if q['kickoff']>finished+60]
    if not packets:return None,dict(audit,noFuturePackets=True)
    keys=sorted({(q['fixture'],q['spec'][0]) for q in packets})
    stamp=datetime.fromtimestamp(finished,timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');target=out/f'frozen_{stamp}.json.gz'
    if target.exists():raise ValueError('Refusing to replace immutable market-first cohort')
    doc=dict(schema=1,candidateId=config['candidateId'],frozenAtUTC=datetime.fromtimestamp(finished,timezone.utc).isoformat(),models=results,
        marketKeys=keys,quotes=packets,bundle=bundle,candidateSha256=digest(CONFIG.read_bytes()),sourceCommit=config['sourceCommit'],
        historySha256=digest(json.dumps(history,sort_keys=True,separators=(',',':')).encode()),futureOutcomesRead=False,
        providerCalls=0,llmCalls=0,refits=0,executionEvidence=False,diagnostics=dict(diagnostics),audit=audit)
    write(target,doc,zipped=True)
    index.setdefault('cohorts',[]).append(dict(path=target.name,sha256=digest(target.read_bytes()),frozenAtUTC=doc['frozenAtUTC'],marketKeys=keys,
        models={k:dict(forecasts=len(v['forecasts']),fixtures=len({r['fixture'] for r in v['forecasts']}),selected=len(v['selected'])) for k,v in results.items()}))
    index['marketKeys']=sorted({tuple(k) for k in index.get('marketKeys',[])}|set(keys))
    return target.name,audit


def score(out,index):
    history=direct.load_actual_results(datetime.now(timezone.utc).timestamp())['GLOBAL']
    actual={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    proofs=read(out/'publication_evidence.json') if (out/'publication_evidence.json').exists() else {}
    accumulated=defaultdict(list);pending=Counter();cohorts=[];seen=set()
    for item in index.get('cohorts',[]):
        raw=(out/item['path']).read_bytes()
        if digest(raw)!=item['sha256']:raise ValueError('Immutable market-first cohort changed')
        doc=json.loads(gzip.decompress(raw));when=datetime.fromisoformat(doc['frozenAtUTC']).timestamp()
        proof=proofs.get(item['path']);verified=None
        if proof:
            if proof['sha256']!=item['sha256']:raise ValueError('Market-first publication proof changed')
            verified=datetime.fromisoformat(proof['publiclyVerifiedAtUTC']).timestamp()
        keys={tuple(k) for k in doc['marketKeys']}
        if seen&keys:raise ValueError('Canonical fixture/metric packets overlap across cohorts')
        seen.update(keys);models={};events={}
        for name,data in doc['models'].items():
            selected=public_rows(data['selected'],verified);forecasts=public_rows(data['forecasts'],verified)
            picks,missing=settled(selected,actual,when);rows,missing_rows=settled(forecasts,actual,when)
            accumulated[name].extend(picks);pending[name]+=missing
            models[name]=dict(selected=canonical_summary(picks),selectedRows=picks,pendingEligible=missing,
                eligibleSelected=len(selected),eligibleFixtures=len({r['fixture'] for r in forecasts}),forecastScored=len(rows),forecastUnscored=missing_rows,
                awaitingPublication=len(data['selected']) if not proof else 0,excludedLatePublication=len(data['selected'])-len(selected) if proof else 0)
            events[name]={(r['fixture'],r['bookmaker'],tuple(r['spec'])):r for r in rows if not r['actualPush']}
        scored=dict(frozenSha256=item['sha256'],scoredAtUTC=datetime.now(timezone.utc).isoformat(),publicationVerified=bool(proof),
            publicationEvidence=proof,models=models,pairedComparisons=paired_comparisons(events,baseline='control_point'),providerCalls=0,llmCalls=0,refits=0)
        write(out/item['path'].replace('.json.gz','_score.json'),scored);cohorts.append(dict(path=item['path'],models=models,publicationVerified=bool(proof)))
    summary=dict(generatedAtUTC=datetime.now(timezone.utc).isoformat(),frozenMarketPackets=len(seen),publicBeforeKickoffFixtures=len({r['fixture'] for item in index.get('cohorts',[]) for data in read(out/item['path'])['models'].values() for r in public_rows(data['forecasts'],datetime.fromisoformat(proofs[item['path']]['publiclyVerifiedAtUTC']).timestamp() if item['path'] in proofs else None)}),
        models={name:dict(settled=canonical_summary(accumulated[name]),pendingEligible=pending[name]) for name in (k+'_'+s for k in KINDS for s in ('point','conservative'))},
        cohorts=cohorts,providerCalls=0,llmCalls=0,refits=0,autoPromotion=False,executionEvidence=False)
    write(out/'summary.json',summary)
    lines=['# Προδημοσιευμένη δοκιμή πρώτης τιμής ανά αγορά','','Κύριος υποψήφιος: coupled_conservative. Όλες οι εκδοχές διατηρούνται. Μόνο δημοσίευση πριν από τη σέντρα επιτρέπει βαθμολόγηση. Εκκρεμείς αγώνες μένουν εκτός του ROI.','',
        '| Εκδοχή | Βαθμολογημένες | Κέρδος | ROI | Εκκρεμείς |','|---|---:|---:|---:|---:|']
    for name,data in summary['models'].items():
        s=data['settled'];roi=f"{s['roi']:+.2%}" if 'roi' in s else '—'
        lines.append(f"| {name} | {s['bets']} | {s.get('profitUnits',0):+.3f} | {roi} | {data['pendingEligible']} |")
    lines+=['','Καμία αυτόματη προαγωγή στο app. Οι παρατηρημένες αποδόσεις δεν είναι αποδείξεις εκτέλεσης. Τα μικρά δείγματα δεν τεκμηριώνουν κερδοφορία.']
    (out/'RESULTS_GR.md').write_text('\n'.join(lines)+'\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--record-publication',action='store_true');p.add_argument('--score-only',action='store_true');args=p.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    lockpath=Path(tempfile.gettempdir())/('market-first-forward-'+digest(str(out.resolve()).encode())[:16]+'.lock')
    with lockpath.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        index=read(out/'index.json') if (out/'index.json').exists() else dict(schema=1,cohorts=[],marketKeys=[])
        if args.record_publication:record_publication(out,index);score(out,index);return
        created=None;audit={};changed=[]
        if not args.score_only:
            config=read(CONFIG);changed=[p for p,h in config['files'].items() if not (ROOT/p).exists() or digest((ROOT/p).read_bytes())!=h]
            if not changed:created,audit=freeze(out,index,config)
            write(out/'index.json',index)
        score(out,index)
        write(out/'status.json',dict(status='COMPLETE',newCohort=created,cohorts=len(index['cohorts']),freezeSkippedForChangedInputs=changed,
            audit=audit,providerCalls=0,llmCalls=0,refits=0))
        print('FORWARD_MARKET_FIRST',created,index['cohorts'][-1] if index['cohorts'] else {},audit,flush=True)


if __name__=='__main__':main()
