"""Archived prematch replay; never fills missing prices or markets.

Only quotes observed on the prediction UTC day, after 03:00 UTC (prior-day
matches have time to finish), are eligible for rolling-state replay. A strict
unique team+kickoff join is not an independently verified numeric-ID crosswalk.
"""
import argparse,gzip,json,sys,hashlib
from collections import Counter,defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.index_quote_receipts import extracted_offers
from betting_v2.opponent_matchup import timestamp
from betting_v2.validated_walk_forward import distribution,zipped
from betting_v2.joint_count import outcome
from betting_v2.recovery_walk_forward import write
from odds_api_io_market_audit import classify_provider_market


def one_snapshot(quotes):
    """Keep the earliest eligible receipt per fixture and bookmaker."""
    earliest={}
    for item in quotes.values():
        key=(item['fixture'],item['bookmaker'])
        candidate=(item['observed'],item['sourceReceipt'])
        earliest[key]=min(earliest.get(key,candidate),candidate)
    return {key:item for key,item in quotes.items()
        if (item['observed'],item['sourceReceipt'])==earliest[item['fixture'],item['bookmaker']]}


def mapped_spec(offer,event):
    family=classify_provider_market(offer['providerMarketName'])['family']
    # Cards count != bookmaker card points; retain diagnostic scoring only.
    if 'CARD' in family or 'ASIAN' in family or 'HANDICAP' in family:return None
    family={'HALF_TIME_RESULT':'HALF_TIME_1X2'}.get(family,family)
    home,away=event.get('home',''),event.get('away','')
    raw=offer['providerMarketName'].lower();team=''
    if family.startswith('TEAM'):
        if ('home' in raw)==('away' in raw):return None
        team=home if 'home' in raw else away
    row=dict(home_team=home,away_team=away,canonical_home_team=home,canonical_away_team=away,
             provider_home_team=home,provider_away_team=away,market=family,
             selection=offer['selection'],line=offer['line'],team=team)
    try:return direct.quote_market(row,home,away)
    except (ValueError,TypeError,OverflowError):return None


def policy(item,by_metric,profile,favourite,legacy):
    spec=item['spec'];metric,scope,side,team,line=spec
    dist=distribution(by_metric[metric],profile);p,push=dist.evaluate(spec);ev=p*item['odd']+push-1
    if not 1.8<item['odd']<3:return None,'ODDS_OUTSIDE_RANGE'
    if p<.6 or ev<=0:return None,'LOW_PROBABILITY_OR_EV'
    confidence=p;preference=1
    fullrow=by_metric.get('GOALS_FT')
    if favourite:
        if fullrow is None:return None,'NO_FAVOURITE_MATCHUP'
        full=distribution(fullrow,profile)
        other='AWAY' if favourite=='HOME' else 'HOME'
        favwin=full.evaluate(('GOALS_FT','RESULT',favourite,'',None))[0]
        oppwin=full.evaluate(('GOALS_FT','RESULT',other,'',None))[0]
        if favwin<=oppwin:return None,'FAVOURITE_MATCHUP_CONTRADICTION'
        attack=metric.startswith('GOALS') or metric in ('CORNERS','SHOTS','SOT')
        if attack:
            h,a=dist.means
            if (h<=a if favourite=='HOME' else a<=h):return None,'FAVOURITE_COUNT_CONTRADICTION'
        conditional=None
        if metric.startswith('GOALS'):
            conditional,_=full.condition(favourite)
            if conditional is not None and 'shares' in by_metric[metric]:
                conditional=conditional.thin(*by_metric[metric]['shares'])
        elif metric in ('CORNERS','SHOTS','SOT'):conditional,_=dist.condition(favourite)
        support=conditional.evaluate(spec)[0] if conditional is not None else None
        if attack and (support is None or support<.6):return None,'LOW_FAVOURITE_SCENARIO_SUPPORT'
        confidence=min(p,support) if support is not None else p
        if legacy:
            if attack and team:
                preference=0 if (team==favourite and side=='OVER') or (team!=favourite and side=='UNDER') else 2
            elif scope in ('RESULT','DC','DNB'):
                good=(favourite,'HOME_OR_DRAW' if favourite=='HOME' else 'AWAY_OR_DRAW','HOME_OR_AWAY')
                preference=0 if side in good else 2
            elif scope=='BTTS':preference=0 if side=='NO' else 2
    tier='HIGH' if confidence>=.8 else 'CONFIRMED' if confidence>=.7 else 'DEVELOPING'
    return dict(item,p=p,push=push,ev=ev,tier=tier,confidence=confidence,preference=preference),None


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args();out=args.output
    history=direct.load_actual_results(timestamp('2026-10-15T00:00:00Z'))['GLOBAL'];bytime=defaultdict(list)
    for r in history:bytime[int(r['_date'])].append(r)
    def identities(value):
        key=direct.name(value);return {v[key] for v in direct.UEFA_ALIASES.values() if key in v}
    counters=Counter();quotes={};triplets=defaultdict(lambda:defaultdict(list))
    for path in sorted((direct.ROOT/'reports/betting_v2/quote_receipts').glob('*.gz')):
        counters['receiptFiles']+=1
        try:
            receipt_bytes=path.read_bytes()
            receipt_hash=hashlib.sha256(receipt_bytes).hexdigest()
            doc=json.loads(gzip.decompress(receipt_bytes));offers,issues=extracted_offers(doc)
        except (ValueError,KeyError,TypeError,OSError):counters['invalidReceipt']+=1;continue
        events={str(v['event'].get('id') or v['event'].get('eventId')):v['event'] for v in doc['boundedMarketSnapshot']}
        for offer in offers:
            ko=timestamp(offer['kickoffUTC']);observed=timestamp(offer['clientReceivedAtUTC'])
            if not timestamp('2026-10-01')<=ko<timestamp('2026-10-15'):continue
            counters['periodOffers']+=1
            # No feature observation after the actual archived quote time.
            day=int(ko//86400)*86400
            if not day+10800<=observed<ko:
                counters['quoteNotSameDayAfter03UTC']+=1;continue
            event=events[offer['providerEventId']];homes,aways=identities(event.get('home')),identities(event.get('away'))
            matches=[r for r in bytime[int(ko)] if r['_home'] in homes and r['_away'] in aways]
            if len(matches)!=1:counters['noUniqueExactJoin']+=1;continue
            row=matches[0];fixture=f"{row['_date']}:{row['_home']}:{row['_away']}"
            spec=mapped_spec(offer,event)
            if not spec:counters['unsupportedOrAmbiguousSettlement']+=1;continue
            key=(fixture,offer['bookmaker'],tuple(spec))
            item=dict(fixture=fixture,bookmaker=offer['bookmaker'],spec=spec,odd=offer['odd'],
                observed=offer['clientReceivedAtUTC'],providerEventId=offer['providerEventId'],
                sourceReceipt=path.name,sourceSha256=receipt_hash,
                snapshot=offer['sourceGenerationId'])
            if key not in quotes or item['observed']<quotes[key]['observed']:quotes[key]=item
            if spec[0]=='GOALS_FT' and spec[1]=='RESULT':
                # Favourite must come from the SAME receipt and bookmaker.
                triplets[path.name,offer['bookmaker'],fixture][spec[2]].append(offer['odd'])
    # Choose one observed receipt per fixture/bookmaker: never combine lines
    # from different times into a synthetic available market snapshot.
    quotes=one_snapshot(quotes)
    zipped(out/'priced_quotes.json.gz',list(quotes.values()));reports={}
    for variant in ('full','no_xg','no_goal_stack'):
        predpath=out/(variant+'_2026-10-01_predictions.json.gz')
        if not predpath.exists():raise ValueError('Missing completed prediction checkpoint: '+variant)
        predictions=defaultdict(dict)
        for r in json.loads(gzip.decompress(predpath.read_bytes())):predictions[r['fixture']][r['metric']]=r
        fitted=json.loads((out/(variant+'_2026-10-01_parameters.json')).read_text())
        for calibration,profile in (('raw',{}),('calibrated',fitted)):
            for legacy in (True,False):
                name=variant+'::'+calibration+'::'+('current_preference' if legacy else 'probability_ranking')
                chosen={};reject=Counter()
                for item in quotes.values():
                    rows=predictions.get(item['fixture'],{});spec=item['spec'];metric=spec[0]
                    if metric not in rows:reject['NO_MODEL_TARGET']+=1;continue
                    prices=triplets[item['sourceReceipt'],item['bookmaker'],item['fixture']]
                    unique={k:next(iter(set(v))) for k,v in prices.items() if len(set(v))==1}
                    fav=None
                    if set(unique)=={'HOME','DRAW','AWAY'}:
                        side=min(unique,key=unique.get)
                        if side!='DRAW' and unique[side]<=1.8:fav=side
                    result,reason=policy(item,rows,profile,fav,legacy)
                    if reason:reject[reason]+=1;continue
                    y,ypush=outcome(*rows[metric]['actual'],spec);result.update(y=y,actualPush=ypush)
                    family='RESULT' if metric.startswith('GOALS') and spec[1] in ('RESULT','DC','DNB') else 'GOALS' if metric.startswith('GOALS') else metric
                    key=(item['fixture'],family)
                    score=(result['preference'],-result['confidence'],-result['p'],-result['ev'],str(spec),item['bookmaker'])
                    if key in chosen:reject['SAME_FIXTURE_MARKET_FAMILY']+=1
                    if key not in chosen or score<chosen[key][0]:chosen[key]=(score,result)
                groups=defaultdict(list)
                for _,r in chosen.values():groups[r['spec'][0]+'::'+r['spec'][1]].append(r)
                stats={}
                for market,rows in groups.items():
                    profits=np.array([r['y']*r['odd']+r['actualPush']-1 for r in rows]);n=len(rows)
                    rng=np.random.default_rng(15102026)
                    clusters=defaultdict(list)
                    for r,profit in zip(rows,profits):clusters[r['fixture']].append(profit)
                    values=list(clusters.values());boot=[]
                    if len(values)>=30:
                        for _ in range(2000):
                            sample=[profit for i in rng.integers(len(values),size=len(values)) for profit in values[i]]
                            boot.append(float(np.mean(sample)))
                    stats[market]=dict(bets=n,wins=sum(r['y'] for r in rows),pushes=sum(r['actualPush'] for r in rows),
                        independentFixtures=len(clusters),
                        hitRate=float(np.mean([r['y'] for r in rows])),roi=float(profits.mean()),yield_=float(profits.mean()),
                        profitUnits=float(profits.sum()),roi95=list(map(float,np.quantile(boot,[.025,.975]))) if boot else None,
                        meanPredicted=float(np.mean([r['p'] for r in rows])),picks=rows)
                reports[name]=dict(byMarket=stats,rejections=dict(reject))
    write(out/'priced_replay.json',dict(counters=dict(counters),uniqueQuotes=len(quotes),reports=reports,providerCalls=0,
        limitations=['Previously inspected periods; not untouched holdout','Observed receipt subset, no complete bookmaker universe',
        'Same-day post-03UTC quote restriction avoids using post-quote rolling features; publication timestamps remain unverified',
        'Unique exact team+kickoff match, no independently verified numeric provider crosswalk',
        'Cards and ambiguous team/period settlement excluded','No execution/slippage evidence; raw tiers are unvalidated probability bands',
        'Python port of current preference logic; not an executed Android end-to-end test']))
    print('COMPLETE_PRICED_REPLAY',len(quotes),dict(counters),flush=True)

if __name__=='__main__':main()
