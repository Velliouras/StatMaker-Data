"""Keep uncertain card outcomes visible and never select a profitable subset."""
import argparse,sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.audit_raw_contract_archives import verify,ROOT
from betting_v2.research_model_revision import read
from betting_v2.research_csv_prices import write
from betting_v2.card_settlement_bounds import count_bounds,settlement_bounds

SOURCE=ROOT/'reports/betting_v2/raw_contract_recovery_20261018'
def main(out):
    binding,signature=verify(out);source=read(SOURCE/'card_label_readiness.json');bounds={};audit=Counter();labels=[]
    for row in source['fixtures']:
        if not row.get('fixture'):continue
        doc=read(ROOT/row['sourcePath']);responses=[]
        from betting_v2.audit_raw_contract_archives import fixture_responses
        for r in fixture_responses(doc):
            if str(r['fixture'].get('id'))==row['apiFootballFixtureId'] and (r['fixture'].get('timestamp')==row['kickoff']):responses.append(r)
        if len(responses)!=1:audit['noUniqueFullCachedResponse']+=1;continue
        result=count_bounds(responses[0]);bounds[row['fixture']]=result;audit['usableCountIntervals' if result['usable'] else 'unusableCountIntervals']+=1
        if result['usable']:audit['exactCountLabels' if result['exact'] else 'nonExactCountIntervals']+=1
        labels.append(dict(fixture=row['fixture'],apiFootballFixtureId=row['apiFootballFixtureId'],kickoff=row['kickoff'],sourcePath=row['sourcePath'],bounds=result))
    results={}
    for packet in ('earliest','latest'):
        rows=[];counts=Counter();fixtures={};quotes=read(SOURCE/f'quotes_{packet}.json.gz')
        for q in quotes:
            if q['spec'][0]!='BET365_CARD_UNITS':continue
            result=settlement_bounds(q['spec'],q['odd'],bounds.get(q['fixture'],{}));rows.append(dict(q,settlementBounds=result));counts['offeredContracts']+=1
            if result['graded']:
                counts['contractsWithCountBounds']+=1;counts['unambiguousContracts' if result['unambiguous'] else 'ambiguousContracts']+=1;fixtures[q['fixture']]=True
            else:counts['ungradedContracts']+=1
        write(out/f'card_contracts_{packet}.json.gz',rows,zipped=True);results[packet]=dict(counts,fixturesWithAnyCountBound=len(fixtures),fixturesWithUnambiguousContract=len({r['fixture'] for r in rows if r['settlementBounds'].get('unambiguous')}))
    write(out/'card_count_bounds.json',dict(audit=dict(audit),fixtures=labels,inputSignature=signature))
    write(out/'summary.json',dict(status='COMPLETE',audit=dict(audit),packets=results,providerCalls=0,llmCalls=0,modelFits=0,bettingSelections=0,inputSignature=signature,
        limitation='No aggregate ROI over these offers is reported. Exact-label availability depends on realized card timing, so the exact-only subset must not be treated as a representative strategy backtest. Uncertainty and missing outcomes remain visible.'))
    print('CARD_COUNT_BOUNDS',dict(audit),results,flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();main(a.output)
