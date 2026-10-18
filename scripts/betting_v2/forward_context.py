"""Independent, immutable prospective evaluation of the contextual count model."""
import argparse,fcntl,gzip,json,math,tempfile
from collections import defaultdict,Counter
from datetime import datetime,timezone
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.forward_candidate import future_quotes,record_publication,public_rows
from betting_v2.index_quote_receipts import _price,PRICE_FIELDS,DIRECTION_FIELDS
from betting_v2.research_context_market import (ROOT,PRIOR,METRICS,MonthlyModels,partitions,prematch_context,
    mean_for,eligible,conservative_winner,feature_vector,predict,forecast,select_scope,canonical_summary)
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_git_archive import absolute_time
from betting_v2.research_market_decisions import fixture_favourite
from betting_v2.score_forward_research import settled,paired_comparisons

CONFIG=ROOT/'reports/betting_v2/forward_context_20261018/candidate.json'
KINDS=('stat','market','simple','context')


def offer_key(event,book,market,selection,selection_id,line,odd):
    return str(event),book,market,selection,str(selection_id or ''),json.dumps(line,sort_keys=True),float(odd)


def market_times(document):
    result=defaultdict(set)
    for item in document['boundedMarketSnapshot']:
        event=item['event'];event_id=event.get('id') or event.get('eventId')
        for book in item['bookmakers']:
            name=str(book.get('bookmaker') or '').strip()
            for market in book.get('markets') or []:
                title=str(market.get('name') or market.get('market') or market.get('type') or market.get('key') or '').strip()
                times={absolute_time(market[k]) for k in ('updatedAt','updated_at','lastUpdatedAt','lastUpdate') if market.get(k) is not None}
                when=next(iter(times)) if len(times)==1 and None not in times else None
                for row in market.get('outcomes') or []:
                    line=row.get('hdp') if row.get('hdp') is not None else row.get('line')
                    label=str(row.get('name') or row.get('label') or row.get('selection') or '').strip();prices=[]
                    for field in PRICE_FIELDS:
                        price=_price(row.get(field))
                        if price is not None:prices.append((label or field,price));break
                    for field in DIRECTION_FIELDS:
                        price=_price(row.get(field))
                        if price is not None:prices.append((field.upper(),price))
                    for selection,price in prices:
                        key=offer_key(event_id,name,title,selection,row.get('id') or row.get('key'),line,price)
                        result[key].add(when)
    return result


def attach_market_times(quotes,directory):
    cache={};result=[];counts=Counter()
    for q in quotes:
        name=q['sourceReceipt'];path=directory/name
        if name not in cache:
            raw=path.read_bytes();cache[name]=(digest(raw),market_times(json.loads(gzip.decompress(raw))))
        sha,lookup=cache[name]
        if sha!=q['sourceSha256']:raise ValueError('Forward receipt hash changed')
        key=offer_key(q['providerEventId'],q['bookmaker'],q['providerMarketName'],q['selection'],q['providerSelectionId'],q.get('line'),q['odd'])
        values=lookup.get(key,set())
        if len(values)!=1 or None in values:counts['missingOrAmbiguousMarketTime']+=1;continue
        when=next(iter(values))
        if not 0<=q['observed']-when<=86400:counts['futureOrStaleMarketTime']+=1;continue
        result.append(dict(q,marketUpdatedAtUTC=datetime.fromtimestamp(when,timezone.utc).isoformat()))
    return result,dict(counts)


def evaluate_group(models,g,parameters,fav):
    qo=g['over'];spec=qo['spec'];m=spec[0];market=g['reference']['q']
    pred=models.prediction(qo['_home'],qo['_away'],m,'coupled')
    if pred is None:return {},{}
    row,profile,prepared=pred['rows'][0],pred['profiles'][0],pred['prepared'][0]
    over=forecast(row,profile,spec,prepared);push=over['pushProbability']
    if push>=1:return {},{}
    context=prematch_context(models,qo,g['margin'],g['reference']['independent'])
    x=feature_vector(qo,over['winProbability']/(1-push),market,mean_for(over,spec),context)
    probabilities=dict(stat=over['winProbability']/(1-push),market=market)
    probabilities.update({kind:predict(x,market,parameters[kind]['point']) for kind in ('simple','context')})
    records=defaultdict(list);options=defaultdict(list)
    for q in (g['over'],g['under']):
        value=over if q['spec'][2]=='OVER' else forecast(row,profile,q['spec'],prepared)
        candidates={}
        for kind,prob in probabilities.items():
            p=(prob if q['spec'][2]=='OVER' else 1-prob)*(1-push)
            records[kind].append(dict(q,winProbability=p,pushProbability=push,status='UNSCORED',
                referenceIndependent=g['reference']['independent'],referenceBooks=g['reference']['books'],contextFeatureVector=x))
            candidate=eligible(q,value,pred['full'],fav,p,push)
            if candidate is not None:candidates[kind]=candidate
        if not candidates:continue
        replicas=[forecast(r,p,q['spec'],pr) for r,p,pr in zip(pred['rows'][1:],pred['profiles'][1:],pred['prepared'][1:])]
        over_replicas=[forecast(r,p,spec,pr) for r,p,pr in zip(pred['rows'][1:],pred['profiles'][1:],pred['prepared'][1:])]
        lowpush=min(push,float(np.quantile([v['pushProbability'] for v in replicas],.1)))
        support=None
        if fav:support=min(value['scenarios'][fav]['winProbability'] or 0.,float(np.quantile([v['scenarios'][fav]['winProbability'] or 0. for v in replicas],.1)))
        for kind,candidate in candidates.items():
            wins=[]
            for i,(v,ov) in enumerate(zip(replicas,over_replicas)):
                if kind=='stat':win=v['winProbability']
                else:
                    if kind=='market':prob=market
                    else:
                        xb=feature_vector(qo,ov['winProbability']/(1-ov['pushProbability']),market,mean_for(ov,spec),context)
                        prob=predict(xb,market,parameters[kind]['replicas'][i])
                    win=(prob if q['spec'][2]=='OVER' else 1-prob)*(1-v['pushProbability'])
                wins.append(win)
            options[kind].append(dict(candidate,_lower=dict(p=min(candidate['p'],float(np.quantile(wins,.1))),push=lowpush,support=support)))
    return records,options


def freeze(out,index,config):
    now=datetime.now(timezone.utc).timestamp();cutoff=math.floor(now/86400)*86400
    history=direct.load_actual_results(cutoff)['GLOBAL']
    quotes,bundle,counters=future_quotes(history,now,set(index.get('events',[])))
    quotes=[q for q in quotes if q['fixture'] not in set(index.get('fixtures',[]))]
    quotes,time_audit=attach_market_times(quotes,ROOT/'reports/betting_v2/quote_receipts')
    groups,partition_audit=partitions(quotes);favs=fixture_favourite(quotes)
    if not groups:return None,dict(counters=counters,timestamps=time_audit,partitions=partition_audit)
    source=ROOT/'reports/betting_v2/context_market_model_20261018'
    saved=read(ROOT/config['parameters']['path'])
    profiles={day:{m:read(PRIOR/f'{m}_{day}_coupled_profiles.json')['profiles'] if day!='2026-10-01'
        else read(ROOT/f'reports/betting_v2/combined_candidate_20261016/{m}_profiles.json') for m in METRICS}
        for day in ('2026-08-01','2026-09-01','2026-10-01')}
    models=MonthlyModels(history,profiles);forecasts=defaultdict(list);options=defaultdict(list)
    for feature_day in sorted({g['over']['featureCutoff'] for g in groups}):
        models.advance(feature_day);parameters=saved['fits'][models.fold]
        if not all(v['point']['fitted'] and v['point']['trainingBefore']<=feature_day for v in parameters.values()):
            raise ValueError('No prior frozen contextual parameter bank')
        for g in groups:
            q=g['over']
            if q['featureCutoff']!=feature_day:continue
            fav=favs.get((q['fixture'],q['bookmaker'],q['sourceReceipt']))
            rows,candidates=evaluate_group(models,g,parameters,fav)
            for k,v in rows.items():forecasts[k].extend(v)
            for k,v in candidates.items():options[k].extend(v)
    finished=datetime.now(timezone.utc).timestamp();results={}
    for kind in KINDS:
        rows=[r for r in forecasts[kind] if r['kickoff']>finished+60]
        winners=select_scope([r for r in options[kind] if r['kickoff']>finished+60])
        for suffix,picks in [('point',winners),('conservative',[r for w in winners if (r:=conservative_winner(w)) is not None])]:
            results[kind+'_'+suffix]=dict(forecasts=rows,selected=picks)
    if not any(v['forecasts'] for v in results.values()):return None,dict(noFuturePredictions=True)
    stamp=datetime.fromtimestamp(finished,timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');target=out/f'frozen_{stamp}.json.gz'
    if target.exists():raise ValueError('Refusing to replace immutable context cohort')
    doc=dict(schema=1,candidateId=config['candidateId'],frozenAtUTC=datetime.fromtimestamp(finished,timezone.utc).isoformat(),
        models=results,bundle=bundle,parameters=config['parameters'],candidateSha256=digest(CONFIG.read_bytes()),
        sourceCommit=config['sourceCommit'],historySha256=digest(json.dumps(history,sort_keys=True,separators=(',',':')).encode()),
        futureOutcomesRead=False,providerCalls=0,llmCalls=0,refits=0,executionEvidence=False,
        uncertainty='Point forecasts and separate conservative selected decisions; no guaranteed lower coverage',
        receiptCounters=counters,timeAudit=time_audit,partitionAudit=partition_audit)
    write(target,doc,zipped=True)
    rows=[r for v in results.values() for r in v['forecasts']]
    index.setdefault('cohorts',[]).append(dict(path=target.name,sha256=digest(target.read_bytes()),frozenAtUTC=doc['frozenAtUTC'],
        models={k:dict(forecasts=len(v['forecasts']),fixtures=len({r['fixture'] for r in v['forecasts']}),selected=len(v['selected'])) for k,v in results.items()}))
    index['events']=sorted(set(index.get('events',[]))|{r['eventIdentity'] for r in rows})
    index['fixtures']=sorted(set(index.get('fixtures',[]))|{r['fixture'] for r in rows})
    return target.name,dict(timeAudit=time_audit,partitionAudit=partition_audit)


def score(out,index):
    history=direct.load_actual_results(datetime.now(timezone.utc).timestamp())['GLOBAL']
    actual={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    proofs=read(out/'publication_evidence.json') if (out/'publication_evidence.json').exists() else {}
    accumulated=defaultdict(list);pending=Counter();cohorts=[];seen=set()
    for item in index.get('cohorts',[]):
        raw=(out/item['path']).read_bytes()
        if digest(raw)!=item['sha256']:raise ValueError('Immutable context cohort changed')
        doc=json.loads(gzip.decompress(raw));when=datetime.fromisoformat(doc['frozenAtUTC']).timestamp()
        proof=proofs.get(item['path']);verified=None
        if proof:
            if proof['sha256']!=item['sha256']:raise ValueError('Context publication proof changed')
            verified=datetime.fromisoformat(proof['publiclyVerifiedAtUTC']).timestamp()
        fixtures={r['fixture'] for d in doc['models'].values() for r in public_rows(d['forecasts'],verified)}
        if seen&fixtures:raise ValueError('Canonical context fixtures overlap across cohorts')
        seen.update(fixtures);models={};events={}
        for name,data in doc['models'].items():
            selected=public_rows(data['selected'],verified);forecasts=public_rows(data['forecasts'],verified)
            picks,missing=settled(selected,actual,when);rows,missing_rows=settled(forecasts,actual,when)
            accumulated[name].extend(picks);pending[name]+=missing
            models[name]=dict(selected=canonical_summary(picks),selectedRows=picks,pendingEligible=missing,
                eligibleSelected=len(selected),eligibleFixtures=len({r['fixture'] for r in forecasts}),forecastScored=len(rows),forecastUnscored=missing_rows,
                awaitingPublication=len(data['selected']) if not proof else 0,excludedLatePublication=len(data['selected'])-len(selected) if proof else 0)
            events[name]={(r['fixture'],r['bookmaker'],tuple(r['spec'])):r for r in rows if not r['actualPush']}
        scored=dict(frozenSha256=item['sha256'],scoredAtUTC=datetime.now(timezone.utc).isoformat(),publicationVerified=bool(proof),
            publicationEvidence=proof,models=models,pairedComparisons=paired_comparisons(events,baseline='market_point'),providerCalls=0,llmCalls=0,refits=0)
        write(out/item['path'].replace('.json.gz','_score.json'),scored);cohorts.append(dict(path=item['path'],models=models,publicationVerified=bool(proof)))
    summary=dict(generatedAtUTC=datetime.now(timezone.utc).isoformat(),publicBeforeKickoffFixtures=len(seen),
        models={name:dict(settled=canonical_summary(accumulated[name]),pendingEligible=pending[name]) for name in (k+'_'+s for k in KINDS for s in ('point','conservative'))},
        cohorts=cohorts,providerCalls=0,llmCalls=0,refits=0,autoPromotion=False,executionEvidence=False)
    write(out/'summary.json',summary)
    lines=['# Προδημοσιευμένη δοκιμή contextual μοντέλου','','Κύριος υποψήφιος: context_conservative. Όλες οι εκδοχές διατηρούνται. Μόνο δημοσίευση πριν από τη σέντρα επιτρέπει βαθμολόγηση. Εκκρεμείς αγώνες μένουν εκτός του ROI.','',
        '| Εκδοχή | Βαθμολογημένες | Κέρδος | ROI | Εκκρεμείς |','|---|---:|---:|---:|---:|']
    for name,data in summary['models'].items():
        s=data['settled'];roi=f"{s['roi']:+.2%}" if 'roi' in s else '—'
        lines.append(f"| {name} | {s['bets']} | {s.get('profitUnits',0):+.3f} | {roi} | {data['pendingEligible']} |")
    lines+=['','Καμία αυτόματη προαγωγή στο app. Οι παρατηρημένες αποδόσεις δεν είναι αποδείξεις εκτέλεσης. Τα μικρά δείγματα δεν τεκμηριώνουν κερδοφορία.']
    (out/'RESULTS_GR.md').write_text('\n'.join(lines)+'\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--record-publication',action='store_true');p.add_argument('--score-only',action='store_true');args=p.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    lockpath=Path(tempfile.gettempdir())/('context-forward-'+digest(str(out.resolve()).encode())[:16]+'.lock')
    with lockpath.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        index=read(out/'index.json') if (out/'index.json').exists() else dict(schema=1,cohorts=[],events=[],fixtures=[])
        if args.record_publication:record_publication(out,index);score(out,index);return
        created=None;audit={};changed=[]
        if not args.score_only:
            config=read(CONFIG);changed=[p for p,h in config['files'].items() if digest((ROOT/p).read_bytes())!=h]
            if not changed:created,audit=freeze(out,index,config)
            write(out/'index.json',index)
        score(out,index)
        write(out/'status.json',dict(status='COMPLETE',newCohort=created,cohorts=len(index['cohorts']),freezeSkippedForChangedInputs=changed,
            audit=audit,providerCalls=0,llmCalls=0,refits=0))
        print('FORWARD_CONTEXT',created,index['cohorts'][-1] if index['cohorts'] else {},audit,flush=True)


if __name__=='__main__':main()
