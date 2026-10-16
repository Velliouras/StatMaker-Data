"""Inspect saved card events and raw offer contracts, without requesting data."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.index_quote_receipts import extracted_offers
from betting_v2.research_scope_expansion import write


def audit(root):
    files=Counter();details=Counter();offers=Counter();examples={};hashes=[]
    for path in sorted((root/'data/api_football/league_tables').glob('events_*.json')):
        raw=path.read_bytes();hashes.append((str(path.relative_to(root)),hashlib.sha256(raw).hexdigest()))
        doc=json.loads(raw);events=doc.get('response',[]);files['eventFiles']+=1
        cards=[e for e in events if e.get('type')=='Card']
        files['eventFilesWithCards']+=bool(cards)
        for e in cards:
            details[e.get('detail','')]+=1
            files['cardsMissingPlayerId']+=not bool((e.get('player') or {}).get('id'))
            files['cardsMissingMinute']+=not isinstance((e.get('time') or {}).get('elapsed'),(int,float))
        fixture_id=str((doc.get('params') or {}).get('fixture') or path.stem.split('_')[-1])
        files['eventFilesWithCachedLineups']+=(root/'data/api_football/league_tables'/('lineups_'+fixture_id+'.json')).exists()
    for path in sorted((root/'reports/betting_v2/quote_receipts').glob('*.gz')):
        try:doc=json.loads(gzip.decompress(path.read_bytes()));rows,_=extracted_offers(doc)
        except (ValueError,KeyError,TypeError,OSError,EOFError):files['invalidQuoteReceipt']+=1;continue
        for r in rows:
            name=r['providerMarketName']
            if not any(t in name.casefold() for t in ('card','booking')):continue
            key=r['bookmaker']+'::'+name;offers[key]+=1
            if key not in examples:examples[key]={k:r.get(k) for k in ('providerMarketName','selection','line','odd','bookmaker','providerEventId')}
    return dict(providerCalls=0,llmCalls=0,cachedEventCoverage=dict(files),eventCardDetails=dict(details),
        rawCardOfferCounts=dict(offers),offerExamples=examples,
        eventArchiveSha256=hashlib.sha256(json.dumps(hashes).encode()).hexdigest(),
        modelsTrained=['YELLOW_CARDS','CARDS','RED_CARDS'],
        observedLabelSemantics=dict(YELLOW_CARDS='HY,AY',CARDS='HY+HR,AY+AR',RED_CARDS='HR,AR'),
        bookmakerRuleEvidence=[dict(bookmaker='Bet365',url='https://help.bet365.gr/s/el-gr/sportsrules/soccer/card-markets',
            checkedDate='2026-10-16',paraphrase='Number-of-cards uses yellow weight one and red weight two, omits second yellows and excludes non-playing people and cards after the match.')],
        pricedReplayStatus='Not graded from aggregate HY+HR: different units and missing player/period eligibility evidence.',
        requiredForExactCardSettlement=['Bookmaker and exact market contract retained on each quote',
            'Eligible on-field player events during regulation, including second-yellow exclusion',
            'Separate model target in the verified bookmaker units; never silently reuse HY+HR'])


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True);result=audit(Path(__file__).resolve().parents[2])
    write(args.output,result);print(json.dumps(result['cachedEventCoverage']),flush=True)

if __name__=='__main__':main()
