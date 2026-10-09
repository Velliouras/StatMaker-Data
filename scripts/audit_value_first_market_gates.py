#!/usr/bin/env python3
"""Read-only market-funnel audit for the *current* immutable App-Ready SQLite bundle.

Does NOT republish candidates, modify a ledger, or claim that a historical
model probability has been recalibrated with today's Android code.
"""
from __future__ import annotations
import argparse,collections,datetime as dt,json,sqlite3,tempfile,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RESULT_MARKETS={'RESULT_1X2','RESULT_DOUBLE_CHANCE'}
DOUBLE_CHANCE={'RESULT_DOUBLE_CHANCE','HT_RESULT_DOUBLE_CHANCE'}

def row_flags(row):
    sub=str(row['sub'] or '')
    odd=row['odd']
    market=row['market']
    posterior=row['posterior']
    model=row['model']
    has_model=model is not None and 0.01<=model<=0.99
    stale_dc=(sub in DOUBLE_CHANCE and row['source']=='no-vig'
              and row['overround'] is not None and row['overround']>1.40)
    historical_value=(odd is not None and market is not None and posterior is not None
        and posterior-market>=.04 and posterior*odd-1>=.05)
    fixture_value=(has_model and market is not None and odd is not None
        and model-market>=.04 and model*odd-1>=.05)
    return {
        'stale_dc':bool(stale_dc), 'has_model':has_model,
        'historical_value':bool(historical_value),
        'fixture_value':bool(fixture_value),
        'required_missing':bool(row['required'] and not has_model),
        'result_market':sub in RESULT_MARKETS,
        'old_premium_zero':bool(sub in RESULT_MARKETS and row['candidate']==1 and row['premium']==0)
    }

def audit(db):
    con=sqlite3.connect(db)
    con.row_factory=sqlite3.Row
    try:
        generations=con.execute("""
            SELECT generation_id,candidate_count,built_at_ms FROM prepared_pattern_generation
            WHERE state='ready' ORDER BY built_at_ms DESC LIMIT 1
        """).fetchall()
        gen=generations[0]['generation_id'] if generations else None
        base="""
        SELECT s.identity_sub_market_key sub, s.selection_odd odd,
          s.bm_market_probability market, s.bm_posterior_probability posterior,
          s.opponent_model_probability model, s.opponent_adjusted_required required,
          s.bm_market_probability_source source, s.bm_market_overround overround,
          CASE WHEN c.selection_key IS NULL THEN 0 ELSE 1 END candidate,
          c.policy_premium_eligible premium, c.recommendation_eligible eligible,
          s.qualifies_pattern pattern
        FROM prepared_selections s
        LEFT JOIN prepared_pattern_candidates c
          ON c.competition_id=s.competition_id AND c.snapshot_version=s.snapshot_version
          AND c.selection_key=s.selection_key AND c.generation_id=?
        WHERE s.selection_odd BETWEEN 1.50 AND 4.50
        """
        grouped=collections.defaultdict(lambda:collections.Counter())
        for row in con.execute(base,(gen,)):
            sub=str(row['sub'] or 'UNMAPPED')
            d=grouped[sub]
            d['selections']+=1
            d['pattern_ready']+=int(row['pattern'] or 0)
            d['indexed_candidate']+=int(row['candidate'] or 0)
            d['candidate_eligible']+=int(row['eligible'] or 0)
            d['premium']+=int(row['premium'] or 0)
            for k,flag in row_flags(row).items():
                d[k]+=int(flag)
        rows={k:dict(v) for k,v in sorted(grouped.items())}
        return {'schemaVersion':1,'dataType':'READ_ONLY_PREPARED_MARKET_FUNNEL_NOT_NEW_PICKS',
                'generatedAt':dt.datetime.now(dt.timezone.utc).isoformat(),
                'generationId':gen,'generationCandidateCount':generations[0]['candidate_count'] if gen else 0,
                'markets':rows,
                'totals':dict(collections.Counter({key:sum(row.get(key,0) for row in rows.values())
                    for key in ('selections','pattern_ready','indexed_candidate','candidate_eligible',
                                'premium','stale_dc','has_model','fixture_value','historical_value','required_missing','old_premium_zero')})),
                'limitations':[
                    'No current-season maturity/convergence columns for all selections: fixture_value is a SHADOW, not a Strong approval',
                    'Existing 1X/X2/12 model estimates built from invalid overround remain contaminated until full model rebuild',
                    'Only stored selection odds 1.50-4.50; unavailable bookmaker markets cannot be audited',
                    'Never use previous candidate premium zero as evidence of a valid negative outcome',
                    'No market weights or model thresholds are changed by this report']}
    finally:con.close()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--manifest',default='data/statmaker/app_ready/update_manifest.json')
    ap.add_argument('--output',default='reports/uat_value_first_market_funnel.json')
    a=ap.parse_args()
    manifest=json.loads((ROOT/a.manifest).read_text())
    bundle=next((ROOT/str(item['path']) for item in manifest['artifacts']
            if item.get('id')=='app_ready_betting_bundle'),None)
    if not bundle or not bundle.is_file():raise SystemExit('CURRENT_BETTING_BUNDLE_MISSING')
    with zipfile.ZipFile(bundle) as z,tempfile.TemporaryDirectory(prefix='statmaker-funnel-') as tmp:
        name=next((x for x in z.namelist() if x.endswith('statmaker_prepared_betting.db')),None)
        if not name:raise SystemExit('PREPARED_SQLITE_MISSING')
        db=Path(tmp)/'prepared.db'
        with db.open('wb') as f:f.write(z.read(name))
        report=audit(db)
    target=ROOT/a.output
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('PREPARED_MARKET_FUNNEL',json.dumps({'counts':report['totals'],
         'generation':report['generationId']},ensure_ascii=False))

if __name__=='__main__':main()
