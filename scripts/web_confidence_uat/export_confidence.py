"""Read-only, device-history-based confidence export. Never reconstructs settlements."""
import argparse
import base64
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from zoneinfo import ZoneInfo
from build_classifier import ROOT, LOCK, build, digest

ENTRY_COLUMNS = ['id','match_date','competition','league_code','market','odd','family',
                 'sub_market_key','model_probability','edge','value_tier','outcome',
                 'settlement_return','prediction_source','source_kind']
SOURCE_KIND = 'PREPARED_FINAL_DAILY_V11'

# Matches Web UAT v4 eligibility. Additional columns supply annotation context only.
PICKS_SQL = """
SELECT c.competition_id, c.snapshot_version, c.selection_key, c.selection_odd,
       s.identity_family, s.selection_market, s.opponent_model_probability,
       s.bm_posterior_probability, s.bm_market_probability, s.bookmaker_evidence_payload,
       s.opponent_adjusted_required, m.payload
FROM prepared_pattern_candidates c
JOIN prepared_selections s ON s.competition_id=c.competition_id
 AND s.snapshot_version=c.snapshot_version AND s.selection_key=c.selection_key
JOIN prepared_matches m ON m.competition_id=c.competition_id
 AND m.snapshot_version=c.snapshot_version AND m.match_key=s.match_key
WHERE c.generation_id=? AND c.recommendation_eligible=1 AND c.selection_odd>=1.50
 AND UPPER(COALESCE(c.value_tier,'')) LIKE '%STRONG%'
 AND UPPER(COALESCE(c.market_family,'')) NOT LIKE '%ASIAN%'
 AND UPPER(COALESCE(c.market_family,'')) NOT LIKE '%HANDICAP%'
 AND UPPER(COALESCE(s.selection_market,'')) NOT LIKE '%ASIAN%'
 AND UPPER(COALESCE(s.selection_market,'')) NOT LIKE '%HANDICAP%'
 AND UPPER(COALESCE(s.selection_name,'')) NOT LIKE '%ASIAN%'
 AND UPPER(COALESCE(s.selection_name,'')) NOT LIKE '%HANDICAP%'
ORDER BY c.competition_id, c.selection_key
"""

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def read_only(path):
    path = Path(path).resolve(strict=True)
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Input contains WAL; use collect_device.py to create a consistent standalone backup')
    db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        db.close()
        raise ValueError('SQLite integrity check failed')
    return db

def finite(value):
    return isinstance(value, (int,float)) and not isinstance(value, bool) and math.isfinite(value)

def tsv_row(values):
    return '\t'.join('~' if value is None else base64.b64encode(str(value).encode()).decode() for value in values)

def prepare_inputs(performance_db, prepared_db, manifest, as_of):
    date.fromisoformat(as_of)
    generation = manifest.get('metadata', {}).get('preparedPatternGenerationId')
    if not generation or not manifest.get('contentVersion'):
        raise ValueError('Missing App-Ready generation/contentVersion')
    perf = read_only(performance_db)
    try:
        rows = [dict(row) for row in perf.execute(
            'SELECT ' + ','.join(ENTRY_COLUMNS) + ' FROM model_recommendations '
            'WHERE source_kind=? AND substr(match_date,1,10)<=? ORDER BY created_at DESC,id ASC',
            (SOURCE_KIND, as_of))]
    finally:
        perf.close()
    if not rows:
        raise ValueError('No canonical Android performance entries; do not infer DEVELOPING from missing data')
    for row in rows:
        for name in ['odd','model_probability','edge','settlement_return']:
            if row[name] is not None and not finite(row[name]):
                raise ValueError(f'Invalid {name} in Android history')
        if row['outcome'] not in {'WON','HALF_WON','HALF_LOST','LOST','VOID','PENDING'}:
            raise ValueError('Unknown settlement outcome')
    db = read_only(prepared_db)
    try:
        state = db.execute('SELECT state FROM prepared_pattern_generation WHERE generation_id=?', (generation,)).fetchone()
        if state is None or str(state[0]).lower() != 'ready':
            raise ValueError('Manifest generation is not READY in the supplied prepared database')
        picks = [dict(row) for row in db.execute(PICKS_SQL, (generation,))]
    finally:
        db.close()
    contexts, unavailable = [], []
    for index, row in enumerate(picks):
        key = {name: row[name] for name in ['competition_id','snapshot_version','selection_key']}
        reason = None
        if not row['identity_family']:
            reason = 'MISSING_PREPARED_MARKET_IDENTITY'
        elif not row['bookmaker_evidence_payload'] or not finite(row['bm_posterior_probability']) or not finite(row['bm_market_probability']):
            reason = 'MISSING_PREPARED_BOOKMAKER_EVIDENCE'
        elif row['opponent_adjusted_required'] and not finite(row['opponent_model_probability']):
            reason = 'ANDROID_RUNTIME_OPPONENT_CONTEXT_REQUIRED'
        if reason:
            unavailable.append({**key, 'status':'unavailable', 'reason':reason})
            continue
        match = json.loads(row['payload'])
        contexts.append({'id':str(index), **key, 'values':[
            str(index), row['identity_family'], row['selection_market'], row['selection_odd'],
            row['opponent_model_probability'], row['bm_posterior_probability'], row['bm_market_probability'],
            match.get('competition',''), match.get('leagueCode','')]})
    return rows, contexts, unavailable, len(picks)

def export(performance_db, prepared_db, manifest_path, android_source, as_of, output, build_dir, dependencies):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Output already exists; use a new UAT directory')
    output_path = output.as_posix()
    if '/data/statmaker/app_ready/' in output_path or '/data/statmaker/app_ready_uat/' in output_path:
        raise ValueError('Export goes to a separate review directory, never an App-Ready publication path')
    paths = [Path(performance_db).resolve(), Path(prepared_db).resolve(), Path(manifest_path).resolve()]
    before = [digest(path.read_bytes()) for path in paths]
    manifest = json.loads(paths[2].read_text())
    entries, contexts, unavailable, candidate_count = prepare_inputs(*paths[:2], manifest, as_of)
    runtime = build(android_source, build_dir, dependencies)
    with tempfile.TemporaryDirectory(prefix='statmaker-confidence-') as temporary:
        temporary = Path(temporary)
        entry_file, context_file = temporary/'entries.tsv', temporary/'contexts.tsv'
        entry_file.write_text('\n'.join(tsv_row([row[c] for c in ENTRY_COLUMNS]) for row in entries)+'\n')
        context_file.write_text('\n'.join(tsv_row(row['values']) for row in contexts)+'\n')
        result = subprocess.run(['java','-Xmx512m','-cp',runtime['classpath'],'com.statmaker.app.RunnerKt',
                                 str(entry_file),str(context_file),as_of], capture_output=True,text=True,check=True,timeout=120)
    by_id = {row['id']:row for row in contexts}
    annotations = []
    for line in result.stdout.splitlines():
        identifier,tier,score,support,positive,negative = line.split('\t')
        row = by_id.pop(identifier)
        if tier not in {'HIGH_CONFIDENCE','CONFIRMED','DEVELOPING'}:
            raise ValueError('Unexpected classifier tier')
        annotations.append({**{k:row[k] for k in ['competition_id','snapshot_version','selection_key']},
                            'status':'classified','tier':tier,'score':float(score),'support':float(support),
                            'positiveSignals':int(positive),'negativeSignals':int(negative)})
    if by_id:
        raise ValueError('Classifier did not return every input context')
    if [digest(path.read_bytes()) for path in paths] != before:
        raise ValueError('Input changed during export; retry using standalone captured databases')
    generated = datetime.now(timezone.utc).isoformat()
    common = {'schemaVersion':1,'scope':'web-confidence-uat','asOfDate':as_of,'generatedAt':generated,
              'androidSourceCommit':LOCK['androidCommit'],'sourceLockSha256':runtime['sourceLockSha256'],
              'contentVersion':manifest['contentVersion'],
              'preparedPatternGenerationId':manifest['metadata']['preparedPatternGenerationId'],
              'historySha256':digest(canonical(entries).encode()),
              'performanceDatabaseSha256':before[0], 'preparedDatabaseSha256':before[1],
              'manifestSha256':before[2], 'historyScope':'ALL_CANONICAL_ANDROID_ENTRIES_AS_OF_DATE',
              'historyEntryCount':len(entries), 'candidateCount':candidate_count,
              'classifiedCount':len(annotations),'unavailableCount':len(unavailable),
              'parityStatus':'EXACT_ANDROID_CLASSIFIER_DEVICE_COMPARISON_REQUIRED',
              'productionReady':False}
    output.mkdir(parents=True)
    history = {**common,'entries':entries}
    payload = {**common,'annotations':annotations+unavailable}
    (output/'settled-history.json').write_text(canonical(history)+'\n',encoding='utf-8')
    (output/'confidence.json').write_text(canonical(payload)+'\n',encoding='utf-8')
    print(json.dumps({k:common[k] for k in ['classifiedCount','unavailableCount','historyEntryCount','parityStatus']}))
    return payload

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['performance-db','prepared-db','manifest','android-source','output','build-dir','dependencies']:
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--as-of-date',required=True,help='Athens date used by Android, YYYY-MM-DD')
    args=parser.parse_args()
    export(args.performance_db,args.prepared_db,args.manifest,args.android_source,args.as_of_date,
           args.output,args.build_dir,args.dependencies)
