"""Rolling, immutable first-receipt forecasts for every remaining app market.

Uses the already frozen goal/period/count banks. No fit, paid call or ROI tuning.
Cards are inventoried but never assigned the generic HY+HR bookmaker outcome.
"""
import argparse,gzip,hashlib,json,math,sqlite3,tempfile,zipfile
from collections import defaultdict,Counter
from datetime import datetime,timezone
from pathlib import Path
from .build import ROOT,read,write
from .catalog import app_market,mapped_spec,families,settlement
from .paper import observed_receipt_times

MODEL='ALL_MARKETS_JOINT_V1'
OUT=ROOT/'reports/market_watch/all_paper_v1'
LEGACY={'MATCH_SHOTS','TEAM_SHOTS','MATCH_SHOTS_ON_TARGET','TEAM_SHOTS_ON_TARGET',
        'MATCH_CORNERS','TEAM_CORNERS','MATCH_FOULS','TEAM_FOULS'}

def collect(history,now,known,horizon):
    import materialize_betting_v2_direct as direct
    from betting_v2.index_quote_receipts import extracted_offers
    from betting_v2.research_quotes import feature_cutoff
    from betting_v2.research_market_decisions import fixture_favourite
    manifest=read(ROOT/'data/statmaker/app_ready/update_manifest.json')
    bundle=next(a for a in manifest['artifacts'] if a['id']=='app_ready_betting_bundle')
    teams={r[k] for r in history for k in ('_home','_away')};catalog=defaultdict(set);audit=Counter();packets={}
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(ROOT/bundle['path']) as archive:archive.extract('databases/statmaker_prepared_betting.db',td)
        with sqlite3.connect(Path(td)/'databases/statmaker_prepared_betting.db') as con:
            con.row_factory=sqlite3.Row
            for item in con.execute('SELECT * FROM prepared_fixture_matches WHERE usable_for_stats=1'):
                f=dict(item);ko=direct.kickoff(f['kickoff'])
                if not now+900<ko<now+horizon*86400:continue
                aliases=direct.UEFA_ALIASES.get(direct.league_code(f['league_code']),{})
                def identity(keys):
                    vals=[aliases.get(direct.name(f[k]),direct.name(f[k])) for k in keys]
                    return next((v for v in vals if v in teams),None)
                h=identity(('canonical_home_team','home_team','provider_home_team'));a=identity(('canonical_away_team','away_team','provider_away_team'))
                if h and a:catalog[str(f['id']),ko].add((h,a))
    # The app's current exact fixture bindings also cover fixtures not yet in the
    # last prepared ZIP. Reuse numeric API fixture + kickoff identity, never fuzzy names.
    from scripts.shots_uat.sources import Identities,UEFA
    identities=Identities(ROOT,history)
    odds=read(ROOT/'odds/odds_api_io/domestic_odds.json',{})
    leagues=list(odds.get('leagues',[]))
    for competition,_ in UEFA:
        source=ROOT/'.sources/uefa' if (ROOT/'.sources/uefa').exists() else ROOT
        doc=read(source/f'odds/odds_api_io/{competition}_odds.json',{})
        if doc:leagues.append(doc)
    for league in leagues:
        for match in league.get('matches',[]):
            ko=direct.kickoff(match.get('kickoff'))
            if not now+900<ko<now+horizon*86400:continue
            bound,reason=identities.bind(league,match)
            if bound is not None:
                _,h,a,api=bound;catalog[str(match['id']),ko].add((h,a))
    audit['catalogFixtures']=len(catalog)
    for path in sorted((ROOT/'reports/betting_v2/quote_receipts').glob('*.gz')):
        try:raw=path.read_bytes();doc=json.loads(gzip.decompress(raw));offers,_=extracted_offers(doc)
        except (ValueError,KeyError,TypeError,OSError,EOFError):audit['invalidReceipt']+=1;continue
        events={str(v['event'].get('id') or v['event'].get('eventId')):v['event'] for v in doc['boundedMarketSnapshot']};packet={}
        for q in offers:
            ko=direct.kickoff(q['kickoffUTC']);obs=direct.kickoff(q['clientReceivedAtUTC']);eid=str(q['providerEventId'])
            if not now-86400<=obs<=now or not now+900<ko<now+horizon*86400:continue
            choices=catalog.get((eid,ko),set())
            if len(choices)!=1:audit['unboundOrAmbiguousIdentity']+=1;continue
            spec=mapped_spec(q,events[eid])
            if spec is None:continue
            market=app_market(spec);h,a=next(iter(choices));fixture=f'{ko}:{h}:{a}'
            if market in LEGACY or (fixture,market) in known:continue
            value=dict(q,fixture=fixture,kickoff=ko,observed=obs,featureCutoff=feature_cutoff(obs),spec=spec,
                appMarket=market,sourceReceipt=path.name,sourceSha256=hashlib.sha256(raw).hexdigest(),_home=h,_away=a,
                eventIdentity=eid+'|'+str(ko),sourceMarket=q['providerMarketName'])
            key=fixture,q['bookmaker'],tuple(spec);old=packet.get(key)
            if old and (old.get('ambiguous') or old['odd']!=value['odd']):value['ambiguous']=True
            packet[key]=value
        rows,times=observed_receipt_times([q for q in packet.values() if not q.get('ambiguous')],ROOT/'reports/betting_v2/quote_receipts')
        audit.update(times)
        # Favourite evidence must be from this very receipt, even though 1X2 has its own first packet.
        result_prices=defaultdict(dict)
        for offer in offers:
            if offer['providerMarketName'].casefold() not in ('ml','1x2','match result'):continue
            side={'HOME':'HOME','DRAW':'DRAW','AWAY':'AWAY','1':'HOME','X':'DRAW','2':'AWAY'}.get(str(offer['selection']).upper())
            if side:result_prices[str(offer['providerEventId']),offer['bookmaker']].setdefault(side,set()).add(offer['odd'])
        for q in rows:
            prices=result_prices[str(q['providerEventId']),q['bookmaker']];fav=None
            if set(prices)=={'HOME','DRAW','AWAY'} and all(len(v)==1 for v in prices.values()):
                odds={k:next(iter(v)) for k,v in prices.items()};side=min(odds,key=odds.get)
                if side!='DRAW' and list(odds.values()).count(odds[side])==1:fav=side
            q['_favourite']=fav
            key=q['fixture'],q['appMarket'];rank=q['observed'],q['sourceReceipt']
            if key not in packets or rank<packets[key]['rank']:packets[key]=dict(rank=rank,rows=[q])
            elif rank==packets[key]['rank']:packets[key]['rows'].append(q)
    return [q for p in packets.values() for q in p['rows']],dict(bundle,actualSha256=hashlib.sha256((ROOT/bundle['path']).read_bytes()).hexdigest()),dict(audit)

def bank(model,home,away,metric):
    from betting_v2.research_app_markets import prepare_extended
    values=model.prepared_bank(home,away,metric)
    if not values:return None
    shares=None
    if metric=='GOALS_FT':
        full=model.predict(home,away,'GOALS_FT');half=model.predict(home,away,'GOALS_1H')
        if half and full:shares=[half[i]/full[i] for i in (0,1)]
    result=[]
    for row,profile,_ in values:
        row=dict(row)
        if shares is not None:row['periodShares']=shares
        result.append((row,profile,prepare_extended(row,profile)))
    return result

def freeze(index,cfg):
    import numpy as np
    import materialize_betting_v2_direct as direct
    from betting_v2.live_revised import load_revised
    from betting_v2.exact_forecast_adapter import forecast
    from betting_v2.research_quotes import assess
    now=datetime.now(timezone.utc).timestamp();history=direct.load_actual_results(math.floor(now/86400)*86400)['GLOBAL']
    quotes,bundle,audit=collect(history,now,set(map(tuple,index['marketKeys'])),cfg['horizonDays'])
    if not quotes:return None,dict(audit,noEligibleQuotes=True)
    forecasts=[];selected=[];blocked=[];diagnostics=Counter();reserved=[]
    for day in sorted({q['featureCutoff'] for q in quotes}):
        model,profile,meta=load_revised(ROOT,history,day);cache={}
        for q in quotes:
            if q['featureCutoff']!=day:continue
            reserved.append((q['fixture'],q['appMarket']))
            if 'CARD' in q['spec'][0]:
                blocked.append(dict(q,reason='BOOKMAKER_SETTLEMENT_CONTRACT_REQUIRED',modelId=MODEL));continue
            key=q['_home'],q['_away'],q['spec'][0]
            if key not in cache:cache[key]=bank(model,*key)
            predictions=cache[key]
            if not predictions:diagnostics['missingPrediction::'+q['appMarket']]+=1;continue
            try:values=[forecast(r,p,q['spec'],prep) for r,p,prep in predictions]
            except ValueError as e:
                if str(e)=='Half allocation missing':diagnostics['missingPeriodAllocation']+=1;continue
                raise
            point,*replicas=values
            if not replicas:raise ValueError('Missing frozen sensitivity bank')
            lower=lambda f:min(float(f(point)),float(np.quantile([f(v) for v in replicas],.1)))
            p=lower(lambda v:v['winProbability']);push=lower(lambda v:v['pushProbability'])
            record=dict(q,winProbability=p,pushProbability=push,pointWinProbability=point['winProbability'],modelId=MODEL,
                        homeMean=point['homeMean'],awayMean=point['awayMean'],status='UNSCORED')
            forecasts.append(record)
            # Existing favourite consistency policy; no new thresholds or market-ROI cherry picking.
            row,pf,prep=predictions[0]
            fullkey=q['_home'],q['_away'],'GOALS_FT'
            if fullkey not in cache:cache[fullkey]=bank(model,*fullkey)
            full=cache[fullkey]
            if full is None:continue
            pick=assess(q,(row,prep,full[0][2][0]),pf,q['_favourite'],'current_preference')
            if pick is None:diagnostics['policyRejected::'+q['appMarket']]+=1;continue
            if p<.60:diagnostics['belowP60LowerBound::'+q['appMarket']]+=1;continue
            if p*q['odd']+push-1<=0:diagnostics['nonPositiveEV::'+q['appMarket']]+=1;continue
            if q['_favourite'] and (q['spec'][0].startswith('GOALS') or q['spec'][0] in ('CORNERS','SHOTS','SOT')):
                if lower(lambda v:v['scenarios'][q['_favourite']]['winProbability'] or 0.)<.60:continue
            selected.append(dict(pick,p=p,push=push,ev=p*q['odd']+push-1,modelId=MODEL))
    # One independent paper decision per app market/fixture; app routing still deduplicates broad families.
    picks={}
    for q in sorted(selected,key=lambda r:(r['preference'],-r['p'],-r['ev'],str(r['spec']),r['bookmaker'])):
        picks.setdefault((q['fixture'],q['appMarket']),q)
    finished=datetime.now(timezone.utc).timestamp();future=lambda r:r['kickoff']>finished+900
    keys=sorted({k for k in reserved if float(k[0].split(':',1)[0])>finished+900})
    if not keys:return None,dict(audit,noFuturePackets=True)
    stamp=datetime.fromtimestamp(finished,timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');name='frozen_'+stamp+'.json.gz'
    doc=dict(schema=2,candidateId=cfg['candidateId'],frozenAtUTC=datetime.fromtimestamp(finished,timezone.utc).isoformat(),
      marketKeys=keys,models={'all_markets_conservative':dict(forecasts=list(filter(future,forecasts)),selected=list(filter(future,picks.values())))},
      blockedContracts=list(filter(future,blocked)),bundle=bundle,candidateSha256=hashlib.sha256((ROOT/'config/market_watch_all_source.json').read_bytes()).hexdigest(),
      sourceCommit=cfg['sourceCommit'],futureOutcomesRead=False,providerCalls=0,llmCalls=0,refits=0,diagnostics=dict(diagnostics),audit=audit)
    path=OUT/name
    if path.exists():raise ValueError('Cannot replace immutable all-market cohort')
    path.write_bytes(gzip.compress(json.dumps(doc,ensure_ascii=False,allow_nan=False).encode(),mtime=0))
    index['cohorts'].append(dict(path=name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),frozenAtUTC=doc['frozenAtUTC'],marketKeys=keys))
    index['marketKeys']=sorted(set(map(tuple,index['marketKeys']))|set(keys))
    return name,dict(audit,forecasts=len(forecasts),selected=len(picks),blockedContracts=len(blocked),diagnostics=dict(diagnostics))

def main():
    import sys
    sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'scripts/betting_v2')]
    from betting_v2.forward_candidate import record_publication
    parser=argparse.ArgumentParser();parser.add_argument('--record-publication',action='store_true');args=parser.parse_args()
    cfg=read(ROOT/'config/market_watch_all_source.json');OUT.mkdir(parents=True,exist_ok=True)
    index=read(OUT/'index.json',dict(schema=2,cohorts=[],marketKeys=[]))
    if args.record_publication:record_publication(OUT,index);return
    changed=[p for p,h in cfg['files'].items() if not (ROOT/p).exists() or hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h]
    name,audit=freeze(index,cfg) if not changed else (None,dict(changedSourceFiles=changed))
    if changed:raise ValueError('Changed all-market frozen inputs: '+str(changed))
    write(OUT/'index.json',index);write(OUT/'status.json',dict(generatedAtUTC=datetime.now(timezone.utc).isoformat(),newCohort=name,audit=audit,providerCalls=0,llmCalls=0,refits=0))
    print('ALL_MARKETS_PAPER',json.dumps(dict(newCohort=name,audit=audit)),flush=True)

if __name__=='__main__':main()
