"""Reobserve exact selections from the latest verified provider receipt."""
import gzip,json,hashlib,re,math
from functools import lru_cache
from .build import ROOT
from .paper import observed_receipt_times


def fresh_uefa_quotes(path, now):
    """Exact normalized quotes only for events confirmed in this successful poll.

    These carry snapshot evidence, not a claim of a raw client HTTP receipt.
    Preserved/rate-limited event rows never inherit the new file timestamp.
    """
    from scripts.shots_uat.sources import timestamp
    if not path.exists():return []
    raw=path.read_bytes();doc=json.loads(raw);debug=doc.get('debug',{})
    observed=timestamp(doc.get('generatedAt'))
    if not now-86400<=observed<=now or debug.get('oddsRefreshSkippedByRateLimit') is not False or debug.get('preservedPreviousExactMarketRows')!=0:return []
    current=set()
    for call in debug.get('apiCalls',[]):
        if call.get('status')==200 and str(call.get('path','')).startswith('/odds'):
            params=call.get('params') or {}
            current.update(str(params.get('eventIds') or params.get('eventId') or '').split(','))
    digest=hashlib.sha256(raw).hexdigest();result=[]
    for match in doc.get('matches',[]):
        kickoff=timestamp(match.get('kickoff'));eid=str(match.get('id',''))
        if eid not in current or kickoff<=now:continue
        for q in match.get('markets',[]):
            if not q.get('exactBookmakerOdds') or not q.get('bookmaker') or not q.get('providerMarketName'):continue
            try:odd=float(q['odds'])
            except (KeyError,TypeError,ValueError):continue
            if not math.isfinite(odd) or odd<=1:continue
            label=str(q.get('selection','')).strip()
            direction=re.search(r'\b(over|under)\b',label,re.I)
            selection=direction[1].upper() if direction else label.upper()
            result.append(dict(providerEventId=eid,kickoff=kickoff,observed=observed,
                bookmaker=q['bookmaker'],providerMarketName=q['providerMarketName'],selection=selection,
                providerSelectionId='',line=q.get('line'),odd=odd,
                sourceReceipt='uefa-snapshot:'+path.name+':'+digest,sourceSha256=digest,
                _providerHome=match.get('providerHomeTeam'),_providerAway=match.get('providerAwayTeam'),
                quoteObservationSemantics='VERIFIED_NORMALIZED_PROVIDER_SNAPSHOT',verifiedClientReceipt=False))
    return result

@lru_cache(maxsize=2)
def current_quotes(now):
    from betting_v2.index_quote_receipts import extracted_offers
    import materialize_betting_v2_direct as direct
    groups={}
    for path in sorted((ROOT/'reports/betting_v2/quote_receipts').glob('*.gz')):
        try:raw=path.read_bytes();doc=json.loads(gzip.decompress(raw));offers,_=extracted_offers(doc)
        except (ValueError,KeyError,TypeError,OSError,EOFError):continue
        quotes=[];events={str(v['event'].get('id') or v['event'].get('eventId')):v['event'] for v in doc['boundedMarketSnapshot']}
        for q in offers:
            obs=direct.kickoff(q['clientReceivedAtUTC']);ko=direct.kickoff(q['kickoffUTC'])
            if now-86400<=obs<=now and ko>now:
                e=events[str(q['providerEventId'])]
                quotes.append(dict(q,observed=obs,kickoff=ko,sourceReceipt=path.name,sourceSha256=hashlib.sha256(raw).hexdigest(),_providerHome=e.get('home'),_providerAway=e.get('away')))
        quotes,_=observed_receipt_times(quotes,ROOT/'reports/betting_v2/quote_receipts')
        for q in quotes:
            key=str(q['providerEventId']),q['kickoff'],q['bookmaker'],q['providerMarketName']
            if key not in groups or q['observed']>groups[key]['observed']:groups[key]=dict(observed=q['observed'],rows=[q])
            elif q['observed']==groups[key]['observed']:groups[key]['rows'].append(q)
    from scripts.shots_uat.sources import UEFA
    source=ROOT/'.sources/uefa' if (ROOT/'.sources/uefa').exists() else ROOT
    for competition,_ in UEFA:
        for q in fresh_uefa_quotes(source/f'odds/odds_api_io/{competition}_odds.json',now):
            key=str(q['providerEventId']),q['kickoff'],q['bookmaker'],q['providerMarketName']
            if key not in groups or q['observed']>groups[key]['observed']:groups[key]=dict(observed=q['observed'],rows=[q])
            elif q['observed']==groups[key]['observed']:groups[key]['rows'].append(q)
    from scripts.shots_uat.primary_quotes import packet
    paths=[ROOT/'odds/odds_api_io/domestic_odds.json']+[source/f'odds/odds_api_io/{competition}_odds.json' for competition,_ in UEFA]
    for path in paths:
        markets,active=packet(path,now)
        if not active:continue
        groups={k:v for k,v in groups.items() if k[0] not in active or (k[2],k[3]) in active[k[0]]}
        for (eid,book,name),blocks in markets.items():
            for market in blocks:
                ko=direct.kickoff(market['_kickoff']);observed=market['_observedAt'];rows=[]
                for odd in market['odds']:
                    for side,price in odd.items():
                        if side=='hdp':continue
                        rows.append(dict(providerEventId=eid,kickoff=ko,observed=observed,bookmaker=book,
                            providerMarketName=name,selection=side.upper(),providerSelectionId='',line=odd.get('hdp'),odd=price,
                            sourceReceipt='api-football-snapshot:'+market['_receiptSha256'],sourceSha256=market['_receiptSha256'],
                            _providerHome=market['_home'],_providerAway=market['_away'],
                            quoteObservationSemantics='API_FOOTBALL_NORMALIZED_SNAPSHOT',verifiedClientReceipt=False))
                groups[eid,ko,book,name]=dict(observed=observed,rows=rows)
    return groups

def bind(q,match,lookup):
    group=lookup.get((str(q['providerEventId']),q['kickoff'],q['bookmaker'],q['sourceMarket']))
    if group is None:return None
    def same(r):
        return (str(r['selection'])==str(q['selection']) and r.get('line')==q.get('line') and str(r.get('providerSelectionId'))==str(q.get('providerSelectionId'))
                and r['_providerHome']==match.get('providerHomeTeam') and r['_providerAway']==match.get('providerAwayTeam'))
    return group['observed'] if {r['odd'] for r in group['rows'] if same(r)}=={q['odd']} else None
