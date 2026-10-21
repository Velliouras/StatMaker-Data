"""Reobserve exact selections from the latest verified provider receipt."""
import gzip,json,hashlib
from functools import lru_cache
from .build import ROOT
from .paper import observed_receipt_times

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
    return groups

def bind(q,match,lookup):
    group=lookup.get((str(q['providerEventId']),q['kickoff'],q['bookmaker'],q['sourceMarket']))
    if group is None:return None
    def same(r):
        return (str(r['selection'])==str(q['selection']) and r.get('line')==q.get('line') and str(r.get('providerSelectionId'))==str(q.get('providerSelectionId'))
                and r['_providerHome']==match.get('providerHomeTeam') and r['_providerAway']==match.get('providerAwayTeam'))
    return group['observed'] if {r['odd'] for r in group['rows'] if same(r)}=={q['odd']} else None
