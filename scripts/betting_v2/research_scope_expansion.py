"""Independent offline market coverage and priced replay; no model/provider calls.

Includes every app market in coverage, scans every saved receipt, records all
join failures and compares earliest/latest complete decision packets. Results
are development diagnostics; no thresholds or live model choices are changed.
"""
import argparse,csv,gzip,hashlib,json,math,re,sys,traceback
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import materialize_betting_v2_direct as direct
from betting_v2.opponent_matchup import timestamp
from betting_v2.index_quote_receipts import extracted_offers
from betting_v2.research_quotes import feature_cutoff,one_fixture_receipt,assess,select,stats
from betting_v2.replay_corrected_prices import mapped_spec
from betting_v2.live_exact import restore
from betting_v2.live_factorized import load_factorized,MANIFEST
from betting_v2.exact_forecast_adapter import prepare
from betting_v2.validated_walk_forward import distribution
from betting_v2.joint_count import outcome

def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.replace(path)

def expanded_spec(offer,event):
    """Add only unambiguous FT foul totals absent from the old classifier."""
    name=offer['providerMarketName'].casefold()
    if 'foul' not in name:return mapped_spec(offer,event)
    if (re.search(r'\b(?:ht|1h|2h)\b',name) or any(t in name for t in
        ('player','half','handicap','spread','most','first','minute'))):return None
    team=''
    if ('home' in name)==('away' in name):
        if 'team' in name:return None
        market='MATCH_FOULS'
    else:
        market='TEAM_FOULS';team=event.get('home' if 'home' in name else 'away','')
    row=dict(home_team=event.get('home',''),away_team=event.get('away',''),
        canonical_home_team=event.get('home',''),canonical_away_team=event.get('away',''),
        provider_home_team=event.get('home',''),provider_away_team=event.get('away',''),
        market=market,selection=offer['selection'],line=offer['line'],team=team)
    try:return direct.quote_market(row,row['home_team'],row['away_team'])
    except (ValueError,TypeError,OverflowError):return None

def feed_bindings(root,identities):
    """Use existing approved provider-ID/alias bindings, with exact kickoff."""
    bindings=defaultdict(set)
    for path in sorted((root/'odds/odds_api_io').glob('*_odds.json')):
        data=json.loads(path.read_text())
        groups=data.get('leagues') or [data]
        for group in groups:
            code=direct.league_code(group.get('leagueCode') or data.get('leagueCode') or '')
            scoped=direct.UEFA_ALIASES.get(code,{})
            for match in group.get('matches',[]):
                if match.get('teamMappingStatus')!='matched' or match.get('usableForStats') is not True:continue
                ko=timestamp(match.get('kickoff'));eid=str(match.get('id') or '')
                h=match.get('canonicalHomeTeam') or match.get('homeTeam');a=match.get('canonicalAwayTeam') or match.get('awayTeam')
                hs={scoped[direct.name(h)]} if direct.name(h) in scoped else identities(h)
                aws={scoped[direct.name(a)]} if direct.name(a) in scoped else identities(a)
                if ko and eid and len(hs)==len(aws)==1:bindings[eid,int(ko)].add((next(iter(hs)),next(iter(aws))))
    return bindings

def app_label(offer,spec):
    raw=offer['providerMarketName'].casefold()
    if spec:
        metric,scope=spec[:2]
        if metric=='GOALS_FT':return {'RESULT':'1X2','DC':'Double Chance','DNB':'Draw No Bet','BTTS':'BTTS','TOTAL':'Match Goals','TEAM':'Team Goals'}[scope]
        if metric=='GOALS_1H':return {'RESULT':'Half-time 1X2','DC':'Half-time Double Chance','TOTAL':'Half-time Goals','TEAM':'Team Goals 1H'}.get(scope,'Half-time Goals')
        if metric=='GOALS_2H':return 'Team Goals 2H' if scope=='TEAM' else 'Goals 2H'
        return {'CORNERS':'Corners','SHOTS':'Shots','SOT':'Shots on Target','FOULS':'Fouls','CARDS':'Cards','YELLOW_CARDS':'Yellow Cards'}[metric]
    rules=[('half time full time','Half-time / Full-time'),('half time / full time','Half-time / Full-time'),
        ('half time/full time','Half-time / Full-time'),('winning margin','Winning Margin'),
        ('correct score','Correct Score'),('both halves','Goal in Both Halves'),('odd/even','Odd / Even Goals'),
        ('odd even','Odd / Even Goals'),('goal band','Goal Bands'),('most corners','Most Corners'),
        ('most shots on target','Most Shots on Target'),('most shots','Most Shots'),
        ('red card','Red Card'),('yellow card','Yellow Cards'),('card','Cards'),('booking','Cards'),('foul','Fouls')]
    for token,label in rules:
        if token in raw:return label
    return 'Outside mapped app scope'

def collect(root,out,history,now):
    bytime=defaultdict(list);byteam=defaultdict(list)
    for r in history:
        bytime[int(r['_date'])].append(r);byteam[r['_home'],r['_away']].append(r['_date'])
    def identities(value):
        key=direct.name(value)
        return {a[key] for a in direct.UEFA_ALIASES.values() if key in a}
    bindings=feed_bindings(root,identities)
    counters=Counter();raw=defaultdict(Counter);events={};packets=defaultdict(dict);rejects={};hashes=[]
    for path in sorted((root/'reports/betting_v2/quote_receipts').glob('*.gz')):
        b=path.read_bytes();hashes.append((path.name,hashlib.sha256(b).hexdigest()));counters['receiptFiles']+=1
        try:doc=json.loads(gzip.decompress(b));offers,_=extracted_offers(doc)
        except (ValueError,KeyError,TypeError,OSError,EOFError):counters['invalidReceipt']+=1;continue
        context={str(v['event'].get('id') or v['event'].get('eventId')):v['event'] for v in doc['boundedMarketSnapshot']}
        for offer in offers:
            eid=offer['providerEventId'];event=context[eid];ko=timestamp(offer['kickoffUTC']);obs=timestamp(offer['clientReceivedAtUTC'])
            if not 0<obs<ko:counters['notPrematch']+=1;continue
            counters['rawPrematchOffers']+=1
            spec=expanded_spec(offer,event);label=app_label(offer,spec)
            bucket=raw[offer['providerMarketName']];bucket['offers']+=1;bucket['mappedOffers']+=int(spec is not None)
            events.setdefault(label,set()).add(eid)
            if ko>=now:counters['futureOffers']+=1;continue
            h,a=identities(event.get('home')),identities(event.get('away'))
            candidates=[r for r in bytime[int(ko)] if r['_home'] in h and r['_away'] in a]
            if len(candidates)!=1 and len(bindings[eid,int(ko)])==1:
                home,away=next(iter(bindings[eid,int(ko)]))
                candidates=[r for r in bytime[int(ko)] if (r['_home'],r['_away'])==(home,away)]
                if len(candidates)==1:counters['recoveredByExistingProviderIdBinding']+=1
            if len(candidates)!=1:
                if not h or not a:reason='UNKNOWN_TEAM_ALIAS'
                elif any((home,away) in byteam for home in h for away in a):reason='EXACT_KICKOFF_MISMATCH'
                else:reason='NO_MATCHED_HISTORICAL_PAIR'
                counters[reason]+=1
                rejects.setdefault((eid,reason),dict(providerEventId=eid,home=event.get('home'),away=event.get('away'),kickoff=ko,reason=reason))
                continue
            counters['exactJoinedOffers']+=1
            if spec is None:counters['unsupportedMarketOrSettlement']+=1;continue
            cutoff=feature_cutoff(obs)
            if cutoff<timestamp('2026-10-01'):counters['beforeFrozenOctoberState']+=1;continue
            r=candidates[0];fixture=f"{r['_date']}:{r['_home']}:{r['_away']}";key=fixture,path.name,offer['bookmaker'];skey=tuple(spec)
            q=dict(fixture=fixture,_home=r['_home'],_away=r['_away'],spec=spec,odd=offer['odd'],observed=obs,kickoff=ko,
                featureCutoff=cutoff,sourceReceipt=path.name,bookmaker=offer['bookmaker'],providerEventId=eid)
            previous=packets[key].get(skey)
            if previous and (previous.get('ambiguous') or previous['odd']!=q['odd']):q['ambiguous']=True
            packets[key][skey]=q
    quotes=[q for packet in packets.values() for q in packet.values() if not q.get('ambiguous')]
    # Retain all raw evidence and both packet choices; never pool them as independent fixtures.
    for mode,latest in [('earliest',False),('latest',True)]:
        chosen=one_fixture_receipt(quotes,latest=latest)
        with gzip.open(out/('quotes_'+mode+'.json.gz'),'wt') as f:json.dump(chosen,f)
    coverage=dict(counters=dict(counters),rawMarkets={k:dict(v) for k,v in sorted(raw.items())},
        appMarketProviderEvents={k:len(v) for k,v in events.items()},
        rejectedUniqueEvents=list(rejects.values()),sourceReceiptSha256=hashlib.sha256(json.dumps(hashes).encode()).hexdigest())
    write(out/'coverage.json',coverage)
    return coverage

def replay(root,out,history):
    truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history}
    manifest=json.loads((root/MANIFEST).read_text());samples=manifest['states']['oof_ridge_50']['sampleCounts']
    state_path=root/'reports/betting_v2/corrected_20261015/full_2026-10-01_pretest_state.json.gz'
    base_state=json.loads(gzip.decompress(state_path.read_bytes()))
    base_profile=json.loads((state_path.parent/'full_2026-10-01_parameters.json').read_text())
    reports={};prediction_cache={}
    for packet in ('earliest','latest'):
        with gzip.open(out/('quotes_'+packet+'.json.gz'),'rt') as f:quotes=json.load(f)
        prices=defaultdict(dict)
        for q in quotes:
            if tuple(q['spec'][:2])==('GOALS_FT','RESULT'):prices[q['fixture'],q['bookmaker'],q['sourceReceipt']][q['spec'][2]]=q['odd']
        options={(name,policy):[] for name in ('full','separate_ft_selected_period')
                 for policy in ('current_preference','probability_ranking','neutral_btts')};missing=Counter()
        for cutoff in sorted({q['featureCutoff'] for q in quotes}):
            composite,profile,_=load_factorized(root,history,cutoff)
            base=restore(base_state,[],timestamp('2026-09-01'),direct.metric_values,sample_counts=samples)
            for r in history:
                if timestamp('2026-10-01')<=r['_date']<cutoff:base.observe(r,direct.metric_values)
            cache={}
            for q in quotes:
                if q['featureCutoff']!=cutoff:continue
                metric=q['spec'][0];actual=direct.metric_values(truth[q['fixture']],metric)
                if actual is None:missing['actualCountUnavailable']+=1;continue
                triplet=prices[q['fixture'],q['bookmaker'],q['sourceReceipt']];fav=None
                if set(triplet)=={'HOME','DRAW','AWAY'}:
                    side=min(triplet,key=triplet.get)
                    if side!='DRAW' and triplet[side]<=1.8:fav=side
                for name,model,p in [('full',base,base_profile),('separate_ft_selected_period',composite,profile)]:
                    key=name,q['_home'],q['_away'],metric
                    if key not in cache:
                        pred=model.predict(q['_home'],q['_away'],metric);full=model.predict(q['_home'],q['_away'],'GOALS_FT')
                        if pred is None or full is None:cache[key]=None
                        else:
                            regime='XG' if len(model.xg[q['_home']])>=8 and len(model.xg[q['_away']])>=8 else 'ELO_NO_XG'
                            row=dict(metric=metric,regime=regime,means=list(pred[:2]))
                            if metric in ('GOALS_1H','GOALS_2H'):row.update(fullMeans=list(full[:2]),shares=[pred[j]/full[j] for j in (0,1)])
                            cache[key]=row,prepare(row,p),distribution(dict(metric='GOALS_FT',regime=regime,means=list(full[:2])),p)
                    prediction=cache[key]
                    if prediction is None:missing[name+'::noPrediction']+=1;continue
                    for policy in ('current_preference','probability_ranking','neutral_btts'):
                        pick=assess(q,prediction,p,fav,policy)
                        if pick is not None:
                            y,push=outcome(*actual,q['spec']);options[name,policy].append(dict(pick,y=y,actualPush=push))
        for (name,policy),rows in options.items():
            picks=select(rows);families=sorted({q['spec'][0]+'::'+q['spec'][1] for q in picks})
            reports[packet+'::'+name+'::'+policy]=dict(overall=stats(picks),
                byMarket={m:stats([q for q in picks if q['spec'][0]+'::'+q['spec'][1]==m]) for m in families},picks=picks)
        write(out/('replay_'+packet+'.json'),dict(reports={k:v for k,v in reports.items() if k.startswith(packet+'::')},
            quotes=len(quotes),fixtures=len({q['fixture'] for q in quotes}),missing=dict(missing),untouchedHoldout=False,
            fittedOrRetunedModels=0,profitabilityValidated=False,quoteExecutionVerified=False))
        write(out/'status.json',dict(stage='REPLAY_'+packet.upper()+'_COMPLETE',providerCalls=0,modelCalls=0))
    return {k:{'overall':v['overall'],'byMarket':v['byMarket']} for k,v in reports.items()}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);ap.add_argument('--audit-only',action='store_true');args=ap.parse_args()
    root=Path(__file__).resolve().parents[2];out=args.output;out.mkdir(parents=True,exist_ok=True)
    write(out/'status.json',dict(stage='STARTED',providerCalls=0,modelCalls=0))
    try:
        now=datetime.now(timezone.utc).timestamp();cutoff=math.floor(now/86400)*86400
        history=direct.load_actual_results(cutoff)['GLOBAL'];coverage=collect(root,out,history,cutoff)
        write(out/'status.json',dict(stage='COVERAGE_COMPLETE',providerCalls=0,modelCalls=0))
        catalogue=json.loads((out/'app_markets.json').read_text());labels=catalogue['labels']
        with (out/'app_market_coverage.csv').open('w',newline='') as f:
            w=csv.writer(f);w.writerow(['app_market','unique_provider_events_observed','needs_attention'])
            for label in labels:w.writerow([label,coverage['appMarketProviderEvents'].get(label,0),
                'Check pricing, settlement and labels separately; zero is not proof the source lacks this market'])
        results={} if args.audit_only else replay(root,out,history)
        summary=dict(status='AUDIT_ONLY_COMPLETE' if args.audit_only else 'COMPLETE',appMarkets=len(labels),
            historyFixtures=len(history),coverage=coverage['counters'],results=results,providerCalls=0,modelCalls=0,
            liveAppChanges=0,untouchedHoldout=False,profitableWinnerSelected=False,
            limitations=['All archive receipts scanned; overlapping packets are not independent samples',
                'No invented prices; cards remain excluded from priced replay',
                'Complex app markets without an implemented exact contract are inventoried, not silently scored',
                'Existing frozen October models only; no retraining or ROI-based tuning',
                'Historical publication times and price execution are unverified',
                'Statistical database rows are not a replacement for historical bookmaker prices'])
        write(out/'summary.json',summary)
        (out/'SUMMARY.md').write_text('# Expanded app-market research\n\n'+
            f"Status: {summary['status']}. App catalogue: {len(labels)} markets. Historical fixtures: {len(history)}.\n\n"+
            'All saved receipts were scanned. Raw market coverage and exact-identity join failures are in coverage.json.\n\n'+
            'Both earliest and latest single-packet fixture decisions are retained. Each is evaluated with frozen baseline/new models and three unchanged policies. All are development comparisons, not independent profitability certification.\n\n'+
            'No AI calls, provider requests, app publication, or live model updates. Full priced results are in replay_earliest.json and replay_latest.json.\n')
        write(out/'status.json',dict(stage=summary['status'],providerCalls=0,modelCalls=0))
        print(json.dumps(summary),flush=True)
    except Exception as exc:
        write(out/'status.json',dict(stage='FAILED',errorType=type(exc).__name__,error=str(exc),providerCalls=0,modelCalls=0))
        raise
if __name__=='__main__':main()
