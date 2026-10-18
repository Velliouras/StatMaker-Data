"""Describe disjoint development periods; keep receipt policies separate."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from betting_v2.research_git_archive import pick_summary

ARMS = ('control_point', 'control_conservative', 'coupled_point', 'coupled_conservative')


def summarize(sources):
    reports = {}
    prior_fixtures = set()
    bindings = []
    documents = []
    for path in sources:
        raw = path.read_bytes()
        doc = json.loads(raw)
        fixtures = {r['fixture'] for arm in ARMS for r in doc['reports'][arm]['picks']}
        overlap = prior_fixtures & fixtures
        if overlap:
            raise ValueError('Development periods overlap: ' + str(sorted(overlap)))
        prior_fixtures.update(fixtures)
        bindings.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)))
        documents.append(doc)
    for arm in ARMS:
        rows = [r for doc in documents for r in doc['reports'][arm]['picks']]
        # Each source has already selected before settlement. Never rerank here.
        reports[arm] = dict(overall=pick_summary(rows), sourceBets=[len(d['reports'][arm]['picks']) for d in documents])
    return dict(sources=bindings, reports=reports, crossPeriodSelectedFixtureOverlap=0)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--prior', type=Path, required=True)
    p.add_argument('--october', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    result = dict(status='DESCRIPTIVE_DEVELOPMENT_ONLY', untouchedHoldout=False,
        rule='Combine disjoint dates within each receipt policy; never combine earliest and latest. No model or selection changes.',
        limitations=['August-September use daily 12:00 UTC archive slots; October uses 12:00 and 23:59 UTC.',
            'Canonical selected fixture overlap is checked across periods, not across arms or receipt policies.',
            'Confidence intervals resample fixtures and do not account for all model selection or temporal dependence.'],
        providerCalls=0, llmCalls=0, results={})
    for packet in ('earliest', 'latest'):
        result['results'][packet] = summarize([args.prior / f'priced_{packet}.json', args.october / f'priced_{packet}.json'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    for packet, report in result['results'].items():
        print(packet, json.dumps({k: v['overall'] for k, v in report['reports'].items()}))


if __name__ == '__main__':
    main()
