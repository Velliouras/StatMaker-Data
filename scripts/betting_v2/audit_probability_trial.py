"""Verify trial rows and make seeded fixture intervals independent of row order."""
import argparse, hashlib, json, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from betting_v2.research_git_archive import pick_summary


def canonical_summary(rows):
    return pick_summary(sorted(rows, key=lambda r:(r['fixture'],str(r['spec']),r['bookmaker'])))


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--source-commit',required=True);args=p.parse_args()
    result=dict(sourceCommit=args.source_commit, sourceSha256={}, results={},
        intervalMethod='Existing 2000 fixed-seed fixture resamples after canonical fixture sorting; not model-selection-adjusted.',
        note='Legacy intervals depend on fixture insertion order through finite Monte Carlo draws. Canonical intervals are invariant to row order; neither establishes stable profitability.')
    for packet in ('earliest','latest'):
        source=args.output/f'priced_{packet}.json'; raw=source.read_bytes();doc=json.loads(raw)
        result['sourceSha256'][source.name]=hashlib.sha256(raw).hexdigest()
        reports={}
        for arm,report in doc['reports'].items():
            rows=report['picks']; keys=[(r['fixture'],r['spec'][0]) for r in rows]
            if len(keys)!=len(set(keys)):raise ValueError('Duplicate fixture/family selection')
            if any(not 1.8<r['odd']<3 or r['p']<.6 or r['p']*r['odd']+r['push']<=1 for r in rows):
                raise ValueError('Original selection thresholds violated')
            summary=canonical_summary(rows)
            if summary['bets']!=report['overall']['bets'] or abs(summary.get('profitUnits',0)-report['overall'].get('profitUnits',0))>1e-10:
                raise ValueError('Published row totals do not reconcile')
            reports[arm]=summary
        result['results'][packet]=dict(baseReproduction=doc['baseReproduction'],reports=reports)
    (args.output/'report_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    for packet,doc in result['results'].items():
        print(packet,{k:(v['bets'],v.get('roi'),v.get('roi95')) for k,v in doc['reports'].items()})


if __name__=='__main__':main()
