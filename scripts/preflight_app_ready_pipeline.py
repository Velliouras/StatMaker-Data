#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sqlite3, subprocess, sys
from pathlib import Path

STATS_CONTRACT='domestic-authoritative-snapshot-v1'
PREPARED_SCHEMA=12
COMPETITIONS={'domestic','champions_league','europa_league','conference_league'}
PATTERN_TABLES={'prepared_pattern_generation','prepared_pattern_candidates'}
PATTERN_INDEXES={'idx_prepared_pattern_generation_ready','idx_prepared_pattern_candidates_scope','idx_prepared_pattern_candidates_rank','idx_prepared_pattern_candidates_competition_rank'}
SOURCE_FILES={'main_manifest.json','domestic_enriched_index.json','domestic.json','uefa_manifest.json','champions_league.json','europa_league.json','conference_league.json'}

def load(path): return json.loads(path.read_text(encoding='utf-8-sig'))
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
    out={}; seen=set(); artifacts=0
    for row in rows:
        c=code(row.get('league_code')); s=season(row.get('app_season')); n=int(row.get('completed_fixtures',0) or 0); key=(s,c)
        if key in seen: r.error(f'duplicate scope {c}@{s}'); continue
        seen.add(key)
        if n>0: out[key]=n
        rel=str(row.get('output_path') or '')
        q=root/rel
        if not rel or not q.is_file(): r.error(f'{c}@{s}: missing artifact {rel}'); continue
        try: matches=load(q).get('matches',[])
        except Exception as e: r.error(f'{c}@{s}: invalid artifact {rel}: {e}'); continue
        artifacts+=1
        if not isinstance(matches,list): r.error(f'{c}@{s}: matches is not a list')
        elif len(matches)!=n: r.error(f'{c}@{s}: index={n} artifact={len(matches)}')
    r.note(f'canonical scopes={len(out)} artifacts={artifacts} matches={sum(out.values())}')
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

def prepared(path,r,label):
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
    run_validator(root,[sys.executable,'scripts/validate_domestic_cache_provider_identity.py'],'provider identity',r)
    run_validator(root,[sys.executable,'scripts/build_app_ready_from_device.py','--validate-domestic-index','data/statmaker/domestic_enriched/index.json'],'enriched index',r)
    for rel in ['data/statmaker/update_manifest.json','odds/odds_api_io/domestic_odds.json','mappings/domestic_team_aliases.json','data/api_football/domestic_normalized_fixture_stats.json']:
        p=root/rel
        if not p.is_file() or p.stat().st_size<=0: r.error(f'missing/empty {rel}'); continue
        try: load(p)
        except Exception as e: r.error(f'invalid JSON {rel}: {e}')
    if private:
        p=private/'app/src/main/java/com/statmaker/app/DomesticApiArtifactImporter.kt'
        text=p.read_text() if p.is_file() else ''
        d=text.find('db.deleteMatchesForLeague(seasonCode, league.leagueCode)'); u=text.find('db.upsertImportedMatches(rows)')
        if d<0 or u<0 or d>u: r.error('staged Domestic importer is not authoritative replace-by-scope')
        else: r.note('staged Domestic importer=authoritative replace-by-scope')
    runner=(root/'scripts/run_app_ready_emulator.sh').read_text()
    if STATS_CONTRACT not in runner: r.error(f'runner missing stats contract {STATS_CONTRACT}')
    else: r.note(f'stats contract={STATS_CONTRACT}')

def checkpoint_mode(root,expected,r):
    if not root.exists(): r.note('no checkpoint restored; clean rebuild'); return
    p=root/'checkpoint.json'
    if not p.is_file(): r.error('checkpoint directory exists without checkpoint.json'); return
    try: meta=load(p)
    except Exception as e: r.error(f'invalid checkpoint.json: {e}'); return
    if int(meta.get('preparedReadyCount',0) or 0)!=4: r.error(f'checkpoint preparedReadyCount={meta.get("preparedReadyCount")} expected=4')
    prepared(root/'databases/statmaker_prepared_betting.db',r,'checkpoint prepared DB')
    contract=str(meta.get('statsProducerContract') or '')
    if contract!=STATS_CONTRACT: r.warn(f'checkpoint stats contract={contract or "<legacy>"}; stats DB will rebuild')
    else:
        actual=stats_counts(root/'databases/statmaker.db',r,'checkpoint stats DB'); problems=compare(expected,actual)
        if problems: r.error('checkpoint claims current stats contract but '+ ' '.join(problems))
        elif actual: r.note(f'checkpoint stats DB matches canonical scopes={len(expected)}')

def generated_mode(root,source,expected,r):
    actual=stats_counts(root/'databases/statmaker.db',r,'generated stats DB'); problems=compare(expected,actual)
    if problems: r.error('generated stats scopes: '+' '.join(problems))
    elif actual: r.note(f'generated stats DB scopes={len(actual)} matches={sum(actual.values())}')
    prepared(root/'databases/statmaker_prepared_betting.db',r,'generated prepared DB')
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
        else: generated_mode(Path(x.generated_root).resolve(),Path(x.source_root).resolve(),expected,r)
    raise SystemExit(r.finish())
if __name__=='__main__': main()
