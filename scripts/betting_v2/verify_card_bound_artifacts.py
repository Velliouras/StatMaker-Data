"""Analytical settlement checks against the saved count-interval evidence."""
import argparse,sys,math
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write,digest

def main(out):
    summary=read(out/'summary.json');results={}
    for packet in ('earliest','latest'):
        rows=read(out/f'card_contracts_{packet}.json.gz');c=Counter();seen=set()
        for r in rows:
            key=r['fixture'],r['bookmaker'],tuple(r['spec']),r['sourceReceipt'];assert key not in seen;seen.add(key);c['offeredContracts']+=1
            v=r['settlementBounds']
            if not v['graded']:c['ungradedContracts']+=1;continue
            lo,hi=v['countLower'],v['countUpper'];assert 0<=lo<=hi and int(lo)==lo and int(hi)==hi;line=r['spec'][4];side=r['spec'][2];expected=set()
            if (hi>line if side=='OVER' else lo<line):expected.add('WIN')
            if (lo<line if side=='OVER' else hi>line):expected.add('LOSS')
            if int(line)==line and lo<=line<=hi:expected.add('PUSH')
            assert expected==set(v['states']);assert v['unambiguous']==(len(expected)==1);profit={'WIN':r['odd']-1,'LOSS':-1.,'PUSH':0.};assert abs(v['profitLower']-min(profit[x] for x in expected))<1e-12 and abs(v['profitUpper']-max(profit[x] for x in expected))<1e-12
            c['contractsWithCountBounds']+=1;c['unambiguousContracts' if len(expected)==1 else 'ambiguousContracts']+=1
        for key,n in c.items():assert summary['packets'][packet][key]==n
        results[packet]=dict(c)
    write(out/'verification.json',dict(status='PASS',results=results,providerCalls=0,llmCalls=0,codeSha256=digest(Path(__file__).read_bytes())));print('CARD_BOUND_MATH_VERIFIED',results)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
