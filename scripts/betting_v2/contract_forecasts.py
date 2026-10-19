"""Restore contract columns from the exact locked market-first writer traversal.

The proper frame omitted spec/book columns. This adapter replays its deterministic
canonical traversal, requires exact identity/outcome/order/length parity, verifies
book-invariant probabilities and checks every saved eligible option. No fuzzy join,
new predictions, unknown labels or outcomes used for betting selection.
"""
from collections import defaultdict
import numpy as np
from betting_v2.research_probability_recalibration import FOLDS
from betting_v2.research_git_archive import absolute_time
from betting_v2.reliable_counts import metric_values
from betting_v2.research_app_markets import outcome_extended


def canonical(q):return q['fixture'],q['spec'][0],q['spec'][1],q['spec'][3],q['spec'][4],q['bookmaker'],q['sourceReceipt']


def restore(quotes,proper,truth,options):
    coverage={(r['fixture'],r['metric']) for r in proper};byday=defaultdict(list)
    for i,q in enumerate(quotes):byday[q['featureCutoff']].append((i,q))
    restored=[];lookup={};coherence={};cursor=0
    for cutoff,rows in sorted(byday.items()):
        seen=set();fold=max(d for d in FOLDS if absolute_time(d+'T00:00:00Z')<=cutoff)
        for i,q in rows:
            key=canonical(q)
            if (q['fixture'],q['spec'][0]) not in coverage or key in seen:continue
            raw=truth.get(q['fixture']);actual=metric_values(raw,q['spec'][0]) if raw is not None else None
            if actual is None:continue
            if cursor>=len(proper):raise ValueError('Contract traversal longer than bound proper frame')
            r=proper[cursor];cursor+=1;y,push=outcome_extended(*actual,q['spec'])
            if q['spec'][2]=='UNDER':y=1-y-push
            if (r['fixture'],r['metric'],r['fold'],r['y'],r['push'])!=(q['fixture'],q['spec'][0],fold,y,push):raise ValueError('Exact proper/contract traversal mismatch')
            ck=(q['fixture'],q['spec'][0],q['spec'][1],q['spec'][3],q['spec'][4])
            if ck in coherence:
                for kind in r['probabilities']:np.testing.assert_allclose(r['probabilities'][kind],coherence[ck][kind],rtol=0,atol=1e-12)
            else:coherence[ck]=r['probabilities']
            item=dict(r,quoteIndex=i,quote=q);restored.append(item);lookup[key]=item;seen.add(key)
    if cursor!=len(proper):raise ValueError('Contract traversal shorter than bound proper frame')
    checked=0
    for kind in ('control','coupled'):
        for q in options.get(kind,[]):
            r=lookup.get(canonical(q))
            if r is None:continue # Unknown actuals have no proper record, remain in options.
            win,push=r['probabilities'][kind];win=win if q['spec'][2]=='OVER' else 1-win-push
            np.testing.assert_allclose([q['p'],q['push']],[win,push],rtol=0,atol=1e-12);checked+=1
    return restored,dict(status='PASS',properEvents=len(proper),restoredEvents=len(restored),bookInvariantContracts=len(coherence),savedEligibleProbabilitiesChecked=checked,method='Exact original-writer canonical traversal and independent outcome/probability parity',fuzzyMatches=0,newModelPredictions=0)
