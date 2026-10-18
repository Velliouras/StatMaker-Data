"""Coverage intersections; no model fitting or return selection."""
import argparse
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.cross_market_inputs import anchors,valid_quote
from betting_v2.research_probability_recalibration import PRIOR,OCTOBER
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write

def audit(quotes,history):
    truth={f"{r['_date']}:{r['_home']}:{r['_away']}":r for r in history};contexts,checks=anchors(quotes)
    months=defaultdict(lambda:defaultdict(set))
    for q in quotes:
        if not valid_quote(q):months[datetime.fromtimestamp(q['kickoff'],timezone.utc).strftime('%Y-%m')][q['spec'][0]].add(q['fixture'])
    bymonth={}
    for month,metrics in sorted(months.items()):
        active={f for f,c in contexts.items() if datetime.fromtimestamp(c['kickoff'],timezone.utc).strftime('%Y-%m')==month}
        bymonth[month]=dict(anchoredFixtures=len(active),metrics={m:dict(quotedFixtures=len(fs),anchoredQuotedFixtures=len(fs&active),
            anchoredQuotedWithStats=sum(f in truth and direct.metric_values(truth[f],m) is not None for f in fs&active)) for m,fs in sorted(metrics.items())})
    times=[c['maxMarketAgeSeconds'] for c in contexts.values()]
    return dict(fixtures=len({q['fixture'] for q in quotes}),quotes=len(quotes),validation=checks,
        anchoredWithCornerStats=sum(f in truth and direct.metric_values(truth[f],'CORNERS') is not None for f in contexts),byMonth=bymonth,
        anchorBookmakers=dict(Counter(v['bookmaker'] for c in contexts.values() for v in c['books'])),
        maxMarketAgeQuantiles=dict(zip(('min','median','p90','max'),map(float,np.quantile(times,[0,.5,.9,1])))) if times else {})

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args();history=read(PRIOR/'history.json.gz');results={}
    for packet in ('earliest','latest'):
        quotes=read(PRIOR/f'quotes_{packet}.json.gz')+read(OCTOBER/f'quotes_{packet}.json.gz');results[packet]=audit(quotes,history)
        print('CROSS_COVERAGE',packet,results[packet]['validation'],flush=True)
    write(args.output,dict(results=results,providerCalls=0,llmCalls=0,labelsUsedForFitting=False,roiUsed=False))

if __name__=='__main__':main()
