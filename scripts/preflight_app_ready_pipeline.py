#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, sqlite3, subprocess, sys
from pathlib import Path

STATS_CONTRACT='domestic-authoritative-snapshot-v1'
RESULT_CONTEXT_ENGINE_CONTRACT='result-context-v1-current-main-retire-asian-handicap'
RESULT_CONTEXT_RULES='pattern-policy-v2-final-read-model-v7-result-context-v1'
PREPARED_SCHEMA=12
COMPETITIONS={'domestic','champions_league','europa_league','conference_league'}
PATTERN_TABLES={'prepared_pattern_generation','prepared_pattern_candidates'}
PATTERN_INDEXES={'idx_prepared_pattern_generation_ready','idx_prepared_pattern_candidates_scope','idx_prepared_pattern_candidates_rank','idx_prepared_pattern_candidates_competition_rank'}
SOURCE_FILES={'main_manifest.json','domestic_enriched_index.json','domestic.json','uefa_manifest.json','champions_league.json','europa_league.json','conference_league.json'}

def load(path): return json.loads(path.read_text(encoding='utf-8-sig'))

def validate_enriched_outputs(root,r):
    p=root/'data/statmaker/domestic_enriched/index.json'
    try: rows=load(p).get('leagues',[])
    except Exception as e:
        r.error(f'enriched index invalid: {e}'); return
    checked=0; bad=[]
    for row in rows:
        output=str(row.get('output_path') or '').strip()
        if not output: continue
        target=Path(output)
        if not target.is_absolute(): target=root/target
        if not target.is_file() or target.stat().st_size<=0:
            bad.append(f"{row.get('league_code') or '?'}:missing_output"); continue
        try: payload=load(target)
        except Exception:
            bad.append(f"{row.get('league_code') or '?'}:invalid_output"); continue
        matches=payload.get('matches',[]) if isinstance(payload,dict) else []
        actual=len(matches) if isinstance(matches,list) else -1
        completed=int(row.get('completed_fixtures',0) or 0)
        checked+=1
        if actual!=completed:
            bad.append(f"{row.get('league_code') or '?'}:{completed}!={actual}")
    if bad: r.error('enriched index/output mismatch: '+','.join(bad[:20]))
    else: r.note(f'enriched index outputs={checked} consistent')
def code(v):
    v=str(v or '').strip().upper(); return 'ROU' if v=='ROM' else v
def season(v):
    v=str(v or '').strip(); return {'2025-2026':'2526','2025 - 2026':'2526','25/26':'2526','2024-2025':'2425','2024 - 2025':'2425','24/25':'2425','2023-2024':'2324','2023 - 2024':'2324','23/24':'2324'}.get(v,v or '2526')

class Report:
    def __init__(self,mode): self.mode=mode; self.errors=[]; self.warnings=[]; self.info=[]
    def error(self,x): self.errors.append(x)
    def warn(self,x): self.warnings.append(x)
    def note(self,x): self.info.append(x)
    def finish(self):
        print(f'APP_READY_AGGREGATE_{self.mode.upper()}_REPORT')
        for x in self.errors: print('ERROR:',x)
        for x in self.warnings: print('WARN:',x)
        for x in self.info: print('INFO:',x)
        print('APP_READY_AGGREGATE_SUMMARY',f'mode={self.mode}',f'errors={len(self.errors)}',f'warnings={len(self.warnings)}',f'info={len(self.info)}')
        return 1 if self.errors else 0

def expected_scopes(root,r):
    p=root/'data/statmaker/domestic_enriched/index.json'
    try: rows=load(p).get('leagues',[])
    except Exception as e: r.error(f'invalid Domestic enriched index: {e}'); return {}
    out={}; seen=set()
    for row in rows:
        c=code(row.get('league_code')); ss=season(row.get('app_season')); n=int(row.get('completed_fixtures',0) or 0); key=(ss,c)
        if key in seen: r.error(f'duplicate scope {c}@{ss}'); continue
        seen.add(key)
        if n>0: out[key]=n
    r.note(f'canonical scopes={len(out)} matches={sum(out.values())}')
    return out

def run_validator(root,cmd,label,r):
    try: p=subprocess.run(cmd,cwd=root,text=True,capture_output=True,timeout=180)
    except Exception as e: r.error(f'{label}: could not run: {e}'); return
    text=' | '.join(x.strip() for x in (p.stdout+'\n'+p.stderr).splitlines() if x.strip())
    if p.returncode: r.error(f'{label}: {text or "failed"}')
    else: r.note(f'{label}: {text.split(" | ")[-1] if text else "ok"}')

def stats_counts(path,r,label):
    if not path.is_file() or path.stat().st_size<=0: r.error(f'{label}: missing/empty {path}'); return {}
    try:
        con=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
        try:
            q=con.execute('PRAGMA quick_check').fetchone()
            if not q or q[0]!='ok': r.error(f'{label}: quick_check={q}')
            rows=con.execute('SELECT season,division,COUNT(*) FROM matches GROUP BY season,division').fetchall()
        finally: con.close()
    except sqlite3.Error as e: r.error(f'{label}: invalid DB: {e}'); return {}
    return {(str(s),code(d)):int(n) for s,d,n in rows}

def compare(expected,actual):
    p=[]; missing=sorted(set(expected)-set(actual)); extra=sorted(set(actual)-set(expected)); mismatch=sorted(k for k in expected.keys()&actual.keys() if expected[k]!=actual[k])
    if missing: p.append('missing='+','.join(f'{c}@{s}' for s,c in missing[:30]))
    if extra: p.append('unexpected='+','.join(f'{c}@{s}' for s,c in extra[:30]))
    if mismatch: p.append('count_mismatch='+','.join(f'{c}@{s}:{expected[k]}!={actual[k]}' for k in mismatch[:30] for s,c in [k]))
    return p

def prepared(path,r,label,require_patterns=True):
    if not path.is_file() or path.stat().st_size<=0: r.error(f'{label}: missing/empty {path}'); return
    try:
        con=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
        try:
            q=con.execute('PRAGMA quick_check').fetchone()
            if not q or q[0]!='ok': r.error(f'{label}: quick_check={q}')
            v=int(con.execute('PRAGMA user_version').fetchone()[0])
            if v<PREPARED_SCHEMA: r.error(f'{label}: schema {v} < {PREPARED_SCHEMA}')
            ready=con.execute("SELECT competition_id,match_count,selection_count FROM prepared_snapshot_meta WHERE state='ready'").fetchall(); names={str(x[0]) for x in ready}
            if names!=COMPETITIONS: r.error(f'{label}: READY={sorted(names)} expected={sorted(COMPETITIONS)}')
            if require_patterns:
                tables={x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}; indexes={x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                if PATTERN_TABLES-tables: r.error(f'{label}: missing tables {sorted(PATTERN_TABLES-tables)}')
                if PATTERN_INDEXES-indexes: r.error(f'{label}: missing indexes {sorted(PATTERN_INDEXES-indexes)}')
                if 'prepared_pattern_candidates' in tables:
                    n=int(con.execute('SELECT COUNT(*) FROM prepared_pattern_candidates').fetchone()[0]);
                    if n<=0: r.error(f'{label}: prepared_pattern_candidates empty')
                    else: r.note(f'{label}: candidates={n}')
            r.note(f'{label}: schema={v} ready='+','.join(f'{a}:{b}/{c}' for a,b,c in sorted(ready)))
        finally: con.close()
    except sqlite3.Error as e: r.error(f'{label}: invalid DB: {e}')

def source_mode(root,private,r):
    required_pipeline = [
        'scripts/materialize_app_ready_pattern_candidates.py',
        'scripts/build_app_ready_from_device.py',
        'scripts/patch_app_ready_clean_current_data.py',
        'scripts/run_app_ready_emulator.sh',
    ]
    for rel in required_pipeline:
        p=root/rel
        if not p.is_file() or p.stat().st_size<=0:
            r.error(f'missing/empty result-context pipeline file {rel}')

    materializer=(root/'scripts/materialize_app_ready_pattern_candidates.py').read_text(encoding='utf-8')
    builder=(root/'scripts/build_app_ready_from_device.py').read_text(encoding='utf-8')
    resume_patch=(root/'scripts/patch_app_ready_clean_current_data.py').read_text(encoding='utf-8')
    if RESULT_CONTEXT_RULES not in materializer:
        r.error('host materializer rules fingerprint is not result-context v7')
    if RESULT_CONTEXT_RULES not in builder:
        r.error('bundle validator rules fingerprint is not result-context v7')
    for forbidden in ('FAMILY_ORDER', 'result_bonus =', 'RESULT_DOUBLE_CHANCE" else 0.0'):
        if forbidden in materializer:
            r.error(f'host materializer still contains market-family ranking bias: {forbidden}')
    if 'value_signal_ranking_score' not in materializer:
        r.error('host materializer does not consume persisted Value ranking score')
    if 'APP_READY_STATMAKER_COMMIT' not in resume_patch or 'statmakerCommit' not in resume_patch:
        r.error('checkpoint compatibility is not bound to the exact StatMaker commit')
    r.note(f'engine contract={RESULT_CONTEXT_ENGINE_CONTRACT} rules={RESULT_CONTEXT_RULES}')

    run_validator(root,[sys.executable,'scripts/validate_domestic_cache_provider_identity.py'],'provider identity',r)
    validate_enriched_outputs(root,r)
    for rel in ['data/statmaker/update_manifest.json','odds/odds_api_io/domestic_odds.json','mappings/domestic_team_aliases.json','data/api_football/domestic_normalized_fixture_stats.json']:
        p=root/rel
        if not p.is_file() or p.stat().st_size<=0: r.error(f'missing/empty {rel}'); continue
        try: load(p)
        except Exception as e: r.error(f'invalid JSON {rel}: {e}')

    if private:
        rules_file=private/'app/src/main/java/com/statmaker/app/PreparedPatternRecommendationModels.kt'
        text=rules_file.read_text(encoding='utf-8') if rules_file.is_file() else ''
        if RESULT_CONTEXT_RULES not in text:
            r.error('selected StatMaker ref is not the result-context v7 recommendation contract')
        else:
            r.note('selected StatMaker ref=result-context v7')
        importer=private/'app/src/main/java/com/statmaker/app/DomesticApiArtifactImporter.kt'
        importer_text=importer.read_text(encoding='utf-8') if importer.is_file() else ''
        if 'db.upsertImportedMatches(rows)' not in importer_text:
            r.error('staged Domestic importer is missing canonical upsert boundary')
        else:
            r.note('staged Domestic importer present; recommendation migration does not alter stats import semantics')

def checkpoint_mode(root,expected,r):
    if not root.exists(): r.note('no checkpoint restored; clean rebuild'); return
    p=root/'checkpoint.json'
    if not p.is_file(): r.error('checkpoint directory exists without checkpoint.json'); return
    try: meta=load(p)
    except Exception as e: r.error(f'invalid checkpoint.json: {e}'); return
    if int(meta.get('preparedReadyCount',0) or 0)!=4: r.error(f'checkpoint preparedReadyCount={meta.get("preparedReadyCount")} expected=4')
    prepared(root/'databases/statmaker_prepared_betting.db',r,'checkpoint prepared DB',require_patterns=False)
    expected_engine=os.environ.get('APP_READY_ENGINE_CONTRACT','')
    expected_commit=os.environ.get('APP_READY_STATMAKER_COMMIT','')
    if expected_engine and str(meta.get('engineContract') or '') != expected_engine:
        r.error(f'checkpoint engine contract={meta.get("engineContract") or "<legacy>"} expected={expected_engine}')
    if expected_commit and str(meta.get('statmakerCommit') or '') != expected_commit:
        r.error('checkpoint StatMaker commit does not match selected engine commit')
    contract=str(meta.get('statsProducerContract') or '')
    if contract!=STATS_CONTRACT: r.warn(f'checkpoint stats contract={contract or "<legacy>"}; stats DB will rebuild')
    else:
        actual=stats_counts(root/'databases/statmaker.db',r,'checkpoint stats DB'); problems=compare(expected,actual)
        if problems: r.error('checkpoint claims current stats contract but '+ ' '.join(problems))
        elif actual: r.note(f'checkpoint stats DB matches canonical scopes={len(expected)}')

def generated_mode(root,source,expected,r,repository_root):
    actual=stats_counts(root/'databases/statmaker.db',r,'generated stats DB')
    if actual:
        r.note(f'generated stats DB scopes={len(actual)} matches={sum(actual.values())}')
    prepared(root/'databases/statmaker_prepared_betting.db',r,'generated prepared DB')
    db=root/'databases/statmaker_prepared_betting.db'
    if db.is_file():
        try:
            con=sqlite3.connect(f'file:{db}?mode=ro',uri=True)
            try:
                bad=int(con.execute("""
                    SELECT COUNT(*)
                    FROM prepared_pattern_candidates c
                    JOIN prepared_selections s
                      ON s.competition_id=c.competition_id
                     AND s.snapshot_version=c.snapshot_version
                     AND s.selection_key=c.selection_key
                    WHERE c.recommendation_eligible=1
                      AND c.competition_id='domestic'
                      AND s.identity_sub_market_key IN (
                        'RESULT_1X2','RESULT_DOUBLE_CHANCE',
                        'HT_RESULT_1X2','HT_RESULT_DOUBLE_CHANCE'
                      )
                      AND (
                        COALESCE(s.opponent_adjusted_required,0)<>1
                        OR s.opponent_model_probability IS NULL
                      )
                """).fetchone()[0])
                if bad:
                    r.error(f'generated result-market context violations={bad}')
                else:
                    r.note('generated domestic result markets have required matchup models')
            finally:
                con.close()
        except sqlite3.Error as e:
            r.error(f'generated result-context validation failed: {e}')
    for name in SOURCE_FILES:
        p=source/name
        if not p.is_file() or p.stat().st_size<=0: r.error(f'source staging missing/empty {name}'); continue
        try: load(p)
        except Exception as e: r.error(f'source staging invalid {name}: {e}')

def main():
    a=argparse.ArgumentParser(); a.add_argument('--mode',choices=['source','checkpoint','generated'],required=True); a.add_argument('--repository-root',default='.'); a.add_argument('--private-root'); a.add_argument('--checkpoint-root'); a.add_argument('--generated-root'); a.add_argument('--source-root'); x=a.parse_args()
    root=Path(x.repository_root).resolve(); r=Report(x.mode); expected=expected_scopes(root,r)
    if x.mode=='source': source_mode(root,Path(x.private_root).resolve() if x.private_root else None,r)
    elif x.mode=='checkpoint':
        if not x.checkpoint_root: r.error('--checkpoint-root required')
        else: checkpoint_mode(Path(x.checkpoint_root).resolve(),expected,r)
    else:
        if not x.generated_root or not x.source_root: r.error('--generated-root and --source-root required')
        else: generated_mode(Path(x.generated_root).resolve(),Path(x.source_root).resolve(),expected,r,root)
    raise SystemExit(r.finish())
if __name__=='__main__': main()
