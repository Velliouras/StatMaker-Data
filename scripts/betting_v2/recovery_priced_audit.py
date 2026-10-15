"""Small archived-price replay of the recovery fold; no live publishing.

Only unique exact resolved team pair + UTC kickoff joins. Unmapped offers and
lines outside the predeclared probability grid are counted, never imputed.
Client receipt time proves an archived observation, not executable availability.
"""
from collections import Counter, defaultdict
from pathlib import Path
import gzip
import json
import sys
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.index_quote_receipts import extracted_offers
from betting_v2.recovery_walk_forward import ts, write


def main():
    root=direct.ROOT; out=root/'reports/betting_v2/recovery_20261015'
    checkpoint=out/'run_v1/2026-10-01.json'
    fold=json.loads(checkpoint.read_text() if checkpoint.exists() else gzip.decompress(checkpoint.with_suffix('.json.gz').read_bytes()))
    predictions=defaultdict(dict)
    for r in fold['events']:
        predictions[r['fixture']][r['metric']+'::'+r['market']]=r
    history=direct.load_actual_results(ts('2026-10-15T08:23:22Z'))['GLOBAL']
    by_time=defaultdict(list)
    for r in history:
        by_time[int(r['_date'])].append(r)
    def identities(value):
        key=direct.name(value)
        return {bindings[key] for bindings in direct.UEFA_ALIASES.values() if key in bindings}
    counters=Counter(); quotes={}
    for path in sorted((root/'reports/betting_v2/quote_receipts').glob('*.gz')):
        counters['receiptFiles']+=1
        try:
            doc=json.loads(gzip.decompress(path.read_bytes())); offers,_=extracted_offers(doc)
        except (ValueError,KeyError,TypeError):
            counters['invalidReceipt']+=1; continue
        events={str(v['event']['id']):v['event'] for v in doc['boundedMarketSnapshot']}
        for offer in offers:
            event=events[offer['providerEventId']]
            kickoff=ts(offer['kickoffUTC'])
            if not ts('2026-10-01T00:00:00Z')<=kickoff<ts('2026-10-15T00:00:00Z'):
                continue
            counters['settledPeriodOfferRows']+=1
            homes,aways=identities(event.get('home')),identities(event.get('away'))
            matches=[r for r in by_time.get(int(kickoff),[]) if r['_home'] in homes and r['_away'] in aways]
            if len(matches)!=1:
                counters['noUniqueExactTeamAndKickoffJoin']+=1; continue
            row=matches[0]; fixture=f"{row['_date']}:{row['_home']}:{row['_away']}"
            market=offer['providerMarketName']; selection=offer['selection']; line=offer['line']
            name=None
            if market=='ML': name='1X2_'+selection
            elif market=='Double Chance': name='DC_'+selection
            elif market=='Both Teams To Score' and selection in ('YES','NO'): name='BTTS_YES'
            elif market in ('Totals','Goals Over/Under') and selection in ('OVER','UNDER'):
                try: name='MATCH_OVER_'+str(float(line))
                except (ValueError,TypeError): pass
            elif market=='Draw No Bet' and selection in ('HOME','AWAY'): name='1X2_'+selection
            if name is None:
                counters['marketOutsidePricedReplayContract']+=1; continue
            prediction=predictions[fixture].get('GOALS_FT::'+name)
            if prediction is None:
                counters['noProbabilityForExactMarketLine']+=1; continue
            item=dict(prediction,bookmaker=offer['bookmaker'],providerEventId=offer['providerEventId'],
                      market=market,selection=selection,line=line,odd=offer['odd'],
                      observed=offer['clientReceivedAtUTC'],push=0.,candidatePush=0.,actualPush=0)
            if selection in ('NO','UNDER'):
                item.update(p=1-item['p'],q=1-item['q'],y=1-item['y'])
            if market=='Draw No Bet':
                draw=predictions[fixture]['GOALS_FT::1X2_DRAW']
                item.update(push=draw['p'],candidatePush=draw['q'],actualPush=draw['y'])
            key=(fixture,item['bookmaker'],market,selection,line)
            # Earliest archived prematch quote, independent of outcome/probability.
            if key not in quotes or item['observed']<quotes[key]['observed']:
                quotes[key]=item
    reports={}
    for label,prob,push in [('baseline','p','push'),('candidate','q','candidatePush')]:
        chosen={}
        for r in quotes.values():
            ev=r[prob]*r['odd']+r[push]-1
            if not (1.8<r['odd']<3. and r[prob]>=.6 and ev>0): continue
            family=('RESULT' if r['market'] in ('ML','Double Chance','Draw No Bet') else
                    'MATCH_GOALS' if r['market'] in ('Totals','Goals Over/Under') else r['market'])
            key=(r['fixture'],family)
            score=(r[prob],ev,r['bookmaker'])
            if key not in chosen or score>chosen[key][0]: chosen[key]=(score,r)
        groups=defaultdict(list)
        for _,r in chosen.values(): groups[r['market']].append(r)
        reports[label]={}
        for market,rows in groups.items():
            returns=np.array([r['y']*r['odd']+r['actualPush']-1 for r in rows])
            fixtures=defaultdict(list)
            for r,value in zip(rows,returns): fixtures[r['fixture']].append(float(value))
            rng=np.random.default_rng(15102026); values=list(fixtures.values()); boot=[]
            for _ in range(2000):
                sample=[v for i in rng.integers(len(values),size=len(values)) for v in values[i]]
                boot.append(float(np.mean(sample)))
            reports[label][market]=dict(bets=len(rows),fixtures=len(fixtures),wins=sum(r['y'] for r in rows),
                hitRate=float(np.mean([r['y'] for r in rows])),flatStakeROI=float(returns.mean()),
                yield_=float(returns.mean()),profitUnits=float(returns.sum()),
                fixtureBootstrapROI95=(list(map(float,np.quantile(boot,[.025,.975]))) if len(fixtures)>=30 else None),
                intervalStatus='insufficient independent fixtures' if len(fixtures)<30 else 'fixture bootstrap',picks=rows)
    result=dict(counters=dict(counters),uniqueArchivedQuotes=len(quotes),byModel=reports,
                retrospectiveOnly=True,providerCalls=0,
                identityJoin='Unique exact resolved home+away+UTC kickoff; cross-provider IDs not independently verified',
                limitations=['Only archived observed subset, not complete historic bookmaker universe',
                 'Only goal 1X2/DC/DNB/BTTS and fixed totals grid supported in priced replay',
                 'Periods were previously inspected; not untouched prospective validation',
                 'Current cached final stats have no historic publication-time guarantee',
                 'No execution/slippage evidence; tiny October sample cannot establish profitable EV'])
    write(out/'priced_audit.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='byModel'}),flush=True)
    print(json.dumps({k:{m:{x:v for x,v in s.items() if x!='picks'} for m,s in markets.items()} for k,markets in reports.items()}),flush=True)


if __name__=='__main__': main()
