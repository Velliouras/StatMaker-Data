"""Recover run-v1 checkpoints, deduplicate event identities, freeze model weights.

This finalizer does not select winning markets or publish picks. Original v1
checkpoints/logs remain the execution evidence; final scores use unique events.
"""
from collections import defaultdict
from pathlib import Path
import gzip
import hashlib
import json
import subprocess
import sys
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import materialize_betting_v2_direct as direct
from betting_v2.recovery_walk_forward import summary, ts, write


def main():
    out=direct.ROOT/'reports/betting_v2/recovery_20261015'
    grouped=defaultdict(list); parameters={}; folds=[]; count=0; seen=set()
    paths={p.name.removesuffix('.gz'):p for p in sorted((out/'run_v1').glob('2026*.json*'))}
    for name,p in sorted(paths.items()):
        payload=gzip.decompress(p.read_bytes()) if p.suffix=='.gz' else p.read_bytes()
        d=json.loads(payload); parameters[name.removesuffix('.json')]=d['parameters']
        folds.append({k:v for k,v in d.items() if k!='events' and k!='parameters'})
        for r in d['events']:
            key=(r['fold'],r['fixture'],r['metric'],r['market'])
            if key in seen: count+=1; continue
            seen.add(key)
            grouped[(r['metric'],r['regime'],r['market'])].append(r)
        # Lossless compressed raw checkpoint with original run-source fingerprint.
        if p.suffix!='.gz':
            zipped=p.with_suffix('.json.gz')
            with gzip.open(zipped,'wb') as f: f.write(payload)
    print('SUMMARIZE',len(seen),'removedDuplicateEventRows',count,flush=True)
    report={'::'.join(k):summary(v) for k,v in grouped.items()}
    write(out/'comparison_unique.json',dict(byMarket=report,folds=folds,removedDuplicateEventRows=count,
          periodsPreviouslyInspected=True,publicationTimeVerified=False,providerCalls=0,
          model='V3 frozen Poisson versus earlier-window mean/dispersion calibration'))
    write(out/'calibration_parameters.json',dict(folds=parameters,productionEnabled=False,
          selectionPolicy='No policy selection from test performance; candidates remain research only'))
    print('FINAL_FIT',flush=True)
    history=direct.load_actual_results(ts('2026-10-15T08:23:22Z'))['GLOBAL']
    model=direct.MatchupLeague(history,ts('2026-10-15T08:23:22Z'))
    weights={}
    for metric,fit in model.models.items():
        weights[metric]=dict(intercept=float(fit.intercept_),coef=fit.coef_.tolist(),
            columns=model.columns[metric],scales=model.feature_scales[metric].tolist())
    if model.period_model is not None:
        weights['GOAL_PERIOD_SPLIT']=dict(intercept=model.period_model.intercept_.tolist(),
            coef=model.period_model.coef_.tolist(),columns=model.period_columns,scales=model.period_scales.tolist())
    source_hash=hashlib.sha256()
    files=[direct.ROOT/'scripts/materialize_betting_v2_direct.py',
           direct.ROOT/'scripts/betting_v2/opponent_matchup.py',direct.ROOT/'scripts/betting_v2/monotone_count.py']
    files+=sorted((direct.ROOT/'data/statmaker/domestic_enriched').glob('*.json'))
    files+=[direct.ROOT/'data/statmaker/uefa_support_history.json',direct.ROOT/'data/statmaker/uefa_team_support_history.json']
    for p in files:
        source_hash.update(str(p.relative_to(direct.ROOT)).encode());source_hash.update(p.read_bytes())
    write(out/'fitted_weights.json',dict(cutoffUTC='2026-10-15T08:23:22Z',training=model.matches,
         snapshotCommit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=direct.ROOT,text=True).strip(),
         inputFingerprint=source_hash.hexdigest(),weights=weights,
         productionEnabled=False,warning='Weights are a fitted baseline checkpoint; calibration is tied to earlier frozen folds, not transplanted to this refit.'))
    # Named feature/history state for reproducibility; no opaque executable pickle.
    with gzip.open(out/'fitted_state.json.gz','wt') as f:
        json.dump(dict(history={k:dict(v) for k,v in model.history.items()},elo=dict(model.elo),
                       xg=dict(model.xg),groups=model.groups),f)
    print('COMPLETE_FIT',model.matches,flush=True)


if __name__=='__main__': main()
