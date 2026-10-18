"""Scan every fixed historical raw snapshot for recoverable foul/card contracts.

Only GitHub archive bytes and existing cached fixture evidence; no provider calls.
"""
import argparse,hashlib,sys,json
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.raw_contract_recovery import recover_snapshot,card_readiness
from betting_v2.research_git_archive import load_snapshot,remove_conflicts,absolute_time
from betting_v2.research_probability_recalibration import ROOT,PRIOR,OCTOBER
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest

def verify(out):
    binding=read(out/'input_bindings.json')
    for p,h in binding['sha256Inputs'].items():
        if digest((ROOT/p).read_bytes())!=h:raise ValueError('Changed recovery input '+p)
    for p,m in binding['gitInputs'].items():
        raw=(ROOT/p).read_bytes();sha=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if len(raw)!=m['size'] or sha!=m['sha']:raise ValueError('Changed cached fixture input '+p)
    return binding,digest((out/'input_bindings.json').read_bytes())

def provider_bindings():
    result=defaultdict(set)
    for source in (PRIOR,OCTOBER):
        for packet in ('earliest','latest'):
            for q in read(source/f'quotes_{packet}.json.gz'):
                result[str(q['providerEventId']),int(q['kickoff'])].add((q['fixture'],q['_home'],q['_away']))
    return result

def join(rows,bindings):
    result=[];c=Counter();start=absolute_time('2026-08-01T00:00:00Z');end=absolute_time('2026-10-16T00:00:00Z')
    for r in rows:
        if not start<=r['kickoff']<end or r['featureCutoff']<start:c['outsideFrozenEvaluationDates']+=1;continue
        choices=bindings.get((str(r['providerEventId']),int(r['kickoff'])),set())
        if len(choices)!=1:c['noUniqueExistingProviderKickoffBinding']+=1;continue
        fixture,h,a=next(iter(choices));result.append(dict(r,fixture=fixture,_home=h,_away=a))
    return result,c

def fixture_responses(value):
    if isinstance(value,dict):
        if isinstance(value.get('fixture'),dict) and isinstance(value.get('teams'),dict):yield value;return
        for v in value.values():yield from fixture_responses(v)
    elif isinstance(value,list):
        for v in value:yield from fixture_responses(v)

def cached_labels(binding,history):
    byid=defaultdict(list)
    for r in history:
        if r.get('fixture_id') is not None:byid[str(r['fixture_id'])].append(r)
    records={};variants=defaultdict(set);ready_units=defaultdict(set);c=Counter();examples={}
    for path in binding['gitInputs']:
        if not any(t in path for t in ('schedule_scorers/fixtures/','domestic_match_context')) and not ('league_tables/' in path and path.endswith('_fixtures.json')):continue
        for response in fixture_responses(read(ROOT/path)):
            fid=str(response['fixture'].get('id'));kickoff=response['fixture'].get('timestamp') or absolute_time(response['fixture'].get('date'));c['fixtureResponses']+=1
            if not fid.isdigit() or not kickoff:c['missingFixtureIdentity']+=1;continue
            readiness=card_readiness(response);key=fid,int(kickoff)
            # Different cache variants are not combined to manufacture missing fields.
            signature=digest(json.dumps(readiness,sort_keys=True).encode());variants[key].add(signature)
            if readiness['ready']:ready_units[key].add(tuple(readiness['provisionalUnits']))
            candidate=dict(apiFootballFixtureId=fid,kickoff=kickoff,sourcePath=path,sourceGitSha=binding['gitInputs'][path]['sha'],readiness=readiness)
            previous=records.get(key)
            if previous is None or (readiness['ready'] and not previous['readiness']['ready']):records[key]=candidate
            matches=[r for r in byid.get(fid,[]) if int(r['_date'])==int(kickoff)]
            for r in matches:
                if len(matches)!=1:continue
                # API fixture ID and exact kickoff must both agree with frozen truth.
                raw_h=(response.get('teams',{}).get('home') or {}).get('name');raw_a=(response.get('teams',{}).get('away') or {}).get('name')
                if not raw_h or not raw_a:continue
                from materialize_betting_v2_direct import name
                if name(raw_h)!=name(r.get('home_team')) or name(raw_a)!=name(r.get('away_team')):c['frozenHistoryApiTeamNameMismatch']+=1;continue
                candidate['fixture']=f"{r['_date']}:{r['_home']}:{r['_away']}";candidate['home']=r['_home'];candidate['away']=r['_away']
    ready=[];allrows=[]
    for key,r in sorted(records.items()):
        r['cachedVariants']=len(variants[key])
        if len(ready_units[key])>1:r['readiness']=dict(r['readiness'],ready=False,flags=sorted(set(r['readiness']['flags'])|{'conflictingReadyCacheLabels'}))
        allrows.append(r);c['uniqueCachedFixtures']+=1
        if r['readiness']['ready']:
            c['readyEventDerivedLabels']+=1
            if r.get('fixture'):ready.append(r);c['readyWithFrozenHistoryFixtureIdAndKickoff']+=1
        else:
            for flag in r['readiness']['flags']:c['blocked::'+flag]+=1
        if r['readiness']['referee']:c['hasRefereeLatestCacheOnly']+=1
    return allrows,ready,dict(c)

def packets(rows,latest):
    grouped=defaultdict(list)
    for r in rows:grouped[r['fixture'],r['spec'][0],r['sourceReceipt']].append(r)
    chosen={}
    for (fixture,metric,receipt),values in grouped.items():
        rank=values[0]['observed'],receipt;key=fixture,metric
        if key not in chosen or (rank>chosen[key][0] if latest else rank<chosen[key][0]):chosen[key]=rank,values
    return [r for _,values in chosen.values() for r in values]

def complete_pairs(rows):
    groups=defaultdict(set)
    for r in rows:groups[r['fixture'],r['sourceReceipt'],r['bookmaker'],r['spec'][0],r['spec'][1],r['spec'][3],r['spec'][4]].add(r['spec'][2])
    full=[k for k,v in groups.items() if v=={'OVER','UNDER'}]
    return dict(partitions=len(full),fixtures=len({k[0] for k in full}))

def main(out,cache):
    binding,signature=verify(out);bridge=provider_bindings();history=read(PRIOR/'history.json.gz');cache.mkdir(parents=True,exist_ok=True)
    cp=out/'scan_checkpoint.json.gz';saved=read(cp) if cp.exists() else dict(signature=signature,done=[],rows=[],audit={},keys={},examples={},sources=[])
    if saved['signature']!=signature:raise ValueError('Recovery checkpoint signature mismatch')
    done=set(saved['done']);rows=saved['rows'];audit=Counter(saved['audit']);keys=Counter(saved['keys']);examples=saved['examples'];sources=saved['sources']
    jobs=[]
    for folder in (PRIOR,OCTOBER):
        manifest=read(folder/'manifest.json')
        for source in manifest['snapshots']:
            if source['sha'] not in done:jobs.append((source,manifest))
    def fetch(job):return load_snapshot(job[0],job[1],cache)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for source,doc in pool.map(fetch,jobs):
            found,a,k,e=recover_snapshot(doc,source);found,conflicts=remove_conflicts(found);audit['conflictingRecoveredQuoteKeys']+=conflicts
            found,j=join(found,bridge);rows+=found;audit.update(a);audit.update(j);keys.update(k)
            for key,value in e.items():examples.setdefault(key,value)
            done.add(source['sha']);sources.append(source)
            write(cp,dict(signature=signature,done=sorted(done),rows=rows,audit=dict(audit),keys=dict(keys),examples=examples,sources=sources),zipped=True)
            write(out/'progress.json',dict(status='SCANNING',snapshotsDone=len(done),snapshotsTotal=85,joinedQuotes=len(rows),providerCalls=0,llmCalls=0))
            print('RAW_ARCHIVE',len(done),source['sha'],len(found),flush=True)
            del doc
    rows,conflicts=remove_conflicts(rows);audit['finalConflicts']+=conflicts
    labels,ready,labelaudit=cached_labels(binding,history);ready_map={r['fixture']:r for r in ready};write(out/'card_label_readiness.json',dict(audit=labelaudit,fixtures=labels,ruleScope='Bet365 FT number/total card units only; latest referee names are not prematch features'))
    summaries={}
    for name,latest in [('earliest',False),('latest',True)]:
        chosen=packets(rows,latest);write(out/f'quotes_{name}.json.gz',chosen,zipped=True);summary={}
        for m in ('FOULS','BET365_CARD_UNITS'):
            subset=[r for r in chosen if r['spec'][0]==m];fixtures={r['fixture'] for r in subset};summary[m]=dict(quotes=len(subset),fixtures=len(fixtures),sides=dict(Counter(r['spec'][2] for r in subset)),completePairs=complete_pairs(subset),cardFixturesWithReadyLabel=len(fixtures&set(ready_map)) if m=='BET365_CARD_UNITS' else None)
        summaries[name]=summary
    write(out/'quotes_all.json.gz',rows,zipped=True)
    write(out/'summary.json',dict(status='COMPLETE',snapshotsVerified=len(done),archivedBytesVerified=sum(r['bytes'] for r in sources),packets=summaries,audit=dict(audit),rawOutcomeKeyInventory=dict(keys),rawExamples=examples,cardLabelAudit=labelaudit,inputSignature=signature,providerCalls=0,llmCalls=0,modelFits=0,appChanges=0,untouchedHoldout=False,
        limitation='Coverage and label readiness, not certified bookmaker settlement or profitability; no ROI-based filtering. Only old fixed 85 archive snapshots and explicitly pinned cached API responses inspected.'))
    print('RAW_RECOVERY_COMPLETE',summaries,labelaudit,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--cache',type=Path,default=Path('/tmp/statmaker-raw-contract-cache'));a=p.parse_args();main(a.output,a.cache)
