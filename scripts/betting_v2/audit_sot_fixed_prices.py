"""Latest archived same-contract prices for fixed earliest SOT decisions only."""
import argparse,math,sys
from collections import defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_probability_recalibration import PRIOR,OCTOBER,ROOT
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.cross_market_inputs import valid_quote
from betting_v2.research_context_market import partitions
from betting_v2.research_git_archive import absolute_time

def key(q):return q['fixture'],str(q['providerEventId']),q['bookmaker'],tuple(q['spec'])

def main(out):
    paths=[source/'quotes_latest.json.gz' for source in (PRIOR,OCTOBER)]
    quotes=[q for p in paths for q in read(p) if q['spec'][0]=='SOT' and valid_quote(q) is None];parts,_=partitions(quotes);index=defaultdict(list)
    for g in parts:
        o,u=g['over'],g['under'];margin=1/o['odd']+1/u['odd']
        for q in (o,u):index[key(q)].append(dict(q,ownConditionalFair=(1/q['odd'])/margin))
    reports=read(out/'priced_earliest.json')['reports'];result={}
    for arm in ('stat_point','stat_conservative','conditional_point','conditional_conservative'):
        picks=reports[arm]['picks'];rows=[];excluded=defaultdict(int)
        for pick in picks:
            candidates=index.get(key(pick),[])
            candidates=[q for q in candidates if q['observed']>pick['observed'] and absolute_time(q['marketUpdatedAtUTC'])>=absolute_time(pick['marketUpdatedAtUTC'])]
            if not candidates:excluded['noLaterCompleteExactContract']+=1;continue
            choices={tuple(q[k] for k in ('odd','ownConditionalFair','observed','marketUpdatedAtUTC')) for q in candidates}
            if len(choices)!=1:excluded['ambiguousLatestQuote']+=1;continue
            q=candidates[0]
            r=dict(fixture=pick['fixture'],bookmaker=pick['bookmaker'],spec=pick['spec'],firstOdd=pick['odd'],latestOdd=q['odd'],firstObserved=pick['observed'],latestObserved=q['observed'],
                firstMarketUpdated=pick['marketUpdatedAtUTC'],latestMarketUpdated=q['marketUpdatedAtUTC'],priceRatio=pick['odd']/q['odd']-1,logPriceRatio=math.log(pick['odd']/q['odd']),ownLatestConditionalFair=q['ownConditionalFair'])
            # This price-implied comparison needs no unknown integer-line push mass.
            r['firstOddTimesLatestFairMinusOne']=pick['odd']*q['ownConditionalFair']-1
            rows.append(r)
        result[arm]=dict(fixedEarliestPicks=len(picks),laterMatched=len(rows),excluded=dict(excluded),
            meanPriceRatio=float(np.mean([r['priceRatio'] for r in rows])) if rows else None,
            meanLogPriceRatio=float(np.mean([r['logPriceRatio'] for r in rows])) if rows else None,
            firstBetter=sum(r['firstOdd']>r['latestOdd'] for r in rows),samePrice=sum(r['firstOdd']==r['latestOdd'] for r in rows),firstWorse=sum(r['firstOdd']<r['latestOdd'] for r in rows),
            meanFirstOddTimesLatestFairMinusOne=float(np.mean([r['firstOddTimesLatestFairMinusOne'] for r in rows])) if rows else None,rows=rows)
    binding={str(p.resolve().relative_to(ROOT)):digest(p.read_bytes()) for p in [*paths,out/'priced_earliest.json',Path(__file__)]}
    write(out/'fixed_earliest_price_audit.json',dict(results=result,inputsSha256=binding,noNewSelection=True,roiUsedForSelection=False,
        limitation='Latest available archived quote, not independently certified closing price or execution. Exact named bookmaker/line only. Incomplete matched subset may be biased. Conditional fair probabilities omit unknown push probability; their sign is informative, not expected stake return.',providerCalls=0,llmCalls=0))
    print({k:{a:b for a,b in v.items() if a not in ('rows','excluded')} for k,v in result.items()},flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
