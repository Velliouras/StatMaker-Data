"""Fixed first-market decisions versus later identical archived contracts.

No outcome labels are used in matching or selection. This is neither a certified
closing line nor an executable return estimate, including on integer lines.
"""
import argparse,sys,math,gzip,json
from pathlib import Path
from collections import defaultdict,Counter
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest
from betting_v2.research_scope_expansion import select_scope
from betting_v2.research_context_market import conservative_winner
from betting_v2.research_git_archive import absolute_time,COUNT_METRICS


def key(q):return q['fixture'],str(q['providerEventId']),q['bookmaker'],tuple(q['spec'])


def stats(rows):
    fair=[r for r in rows if r['ownLatestConditionalFair'] is not None]
    return dict(matched=len(rows),firstBetter=sum(r['firstOdd']>r['latestOdd'] for r in rows),samePrice=sum(r['firstOdd']==r['latestOdd'] for r in rows),firstWorse=sum(r['firstOdd']<r['latestOdd'] for r in rows),
        meanPriceRatio=float(np.mean([r['priceRatio'] for r in rows])) if rows else None,
        completeOwnBookPairs=len(fair),meanFirstOddTimesLatestFairMinusOne=float(np.mean([r['firstOddTimesLatestFairMinusOne'] for r in fair])) if fair else None)


def main(out):
    latest=read(out/'quotes_latest.json.gz');index=defaultdict(list)
    for q in latest:index[key(q)].append(q)
    options=read(out/'replay_checkpoint_earliest.json.gz')['options'];results={}
    for kind in ('control','coupled'):
        point=select_scope(options[kind])
        for mode,picks in [('point',point),('conservative',[r for w in point if (r:=conservative_winner(w)) is not None])]:
            rows=[];excluded=Counter()
            for p in picks:
                candidates=[q for q in index[key(p)] if q['observed']>p['observed'] and absolute_time(q['marketUpdatedAtUTC'])>=absolute_time(p['marketUpdatedAtUTC'])]
                unique={(q['odd'],q['observed'],q['marketUpdatedAtUTC'],q['sourceReceipt']) for q in candidates}
                if len(unique)!=1:excluded['noLaterExactContract' if not candidates else 'ambiguousLatestQuote']+=1;continue
                q=candidates[0];opposite=list(q['spec']);opposite[2]='UNDER' if opposite[2]=='OVER' else 'OVER'
                other=index.get((q['fixture'],str(q['providerEventId']),q['bookmaker'],tuple(opposite)),[])
                other=[r for r in other if r['sourceReceipt']==q['sourceReceipt'] and r['observed']==q['observed'] and r['marketUpdatedAtUTC']==q['marketUpdatedAtUTC']]
                prices={r['odd'] for r in other};fair=None
                if len(prices)==1:
                    margin=1/q['odd']+1/next(iter(prices))
                    if .95<=margin<=1.25:fair=(1/q['odd'])/margin
                r=dict(fixture=p['fixture'],metric=p['spec'][0],providerEventId=p['providerEventId'],bookmaker=p['bookmaker'],spec=p['spec'],firstOdd=p['odd'],latestOdd=q['odd'],firstObserved=p['observed'],latestObserved=q['observed'],firstReceipt=p['sourceReceipt'],latestReceipt=q['sourceReceipt'],priceRatio=p['odd']/q['odd']-1,ownLatestConditionalFair=fair,firstOddTimesLatestFairMinusOne=None if fair is None else p['odd']*fair-1)
                assert r['latestObserved']>r['firstObserved'] and r['firstOdd']>1 and r['latestOdd']>1
                rows.append(r)
            results[kind+'_'+mode]=dict(fixedEarliestPicksIncludingUnknownOutcomes=len(picks),**stats(rows),excluded=dict(excluded),byMetric={m:stats([r for r in rows if r['metric']==m]) for m in COUNT_METRICS},rows=rows)
    inputs=[out/'quotes_latest.json.gz',out/'replay_checkpoint_earliest.json.gz',Path(__file__)]
    document=dict(results=results,inputsSha256={p.name:digest(p.read_bytes()) for p in inputs},noNewSelection=True,noOutcomeConditioning=True,roiUsedForSelection=False,providerCalls=0,llmCalls=0,
        limitation='Latest per-market archived packet, not independently verified closing or execution. Exact provider/fixture/bookmaker/line/side only. Matched subset may be biased. Fair probability is own-book proportional no-vig conditional on no push, not an expected stake return for integer lines.')
    write(out/'fixed_earliest_price_audit.json',document)
    (out/'fixed_earliest_price_audit.json.gz').write_bytes(gzip.compress((out/'fixed_earliest_price_audit.json').read_bytes(),mtime=0))
    write(out/'fixed_earliest_price_summary.json',dict(document,results={k:{a:b for a,b in v.items() if a!='rows'} for k,v in results.items()}))
    print({k:{a:b for a,b in v.items() if a not in ('rows','byMetric')} for k,v in results.items()})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
