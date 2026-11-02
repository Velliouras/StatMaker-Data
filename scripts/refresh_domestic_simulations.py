"""Refresh only changed domestic season simulations using cached verified inputs."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import materialize_prepared_league_simulations as sim
from hotpatch_app_ready_simulation import sha256
from simulation_precision import SEASON_SIMULATION_RUNS
from simulation_priority_inputs import verified_points

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'data/statmaker/app_ready/update_manifest.json'
REPORT = 'reports/domestic_simulation_refresh.json'
FP_KEY = 'preparedDomesticLeagueSimulationFingerprints'
SHA_KEY = 'preparedDomesticLeagueSimulationValidatedBundleSha256'
TABLES = {'prepared_league_simulation_meta','prepared_league_team_simulations','prepared_league_standing_zones'}


def artifact(manifest):
    return next(a for a in manifest['artifacts'] if a['id'] == 'app_ready_betting_bundle')


def fingerprint(entry, con, results_only=False):
    payload = sim.load_fixture_payload(entry)
    if payload is None:
        raise ValueError('Missing simulation fixture cache: '+entry['league_code'])
    fixtures = sim.current_season_fixtures(entry, payload)
    scope = sim.load_elo_scope(con, entry['league_code'], str(entry.get('app_season') or entry.get('target_app_season')))
    if scope is None:
        raise ValueError('Missing prepared Elo for '+entry['league_code'])
    keys = ('fixture_id','date','date_utc','home_team','away_team','status','home_goals','away_goals',
            'goals','normalized_stats','source_league')
    if results_only:
        keys = tuple(k for k in keys if k != 'normalized_stats')
    value = {'entry':{k:entry.get(k) for k in ('league_code','app_season','target_app_season',
        'api_football_season','target_season_start','target_season_end')},
        'fixtures':[{k:f.get(k) for k in keys} for f in fixtures], 'elo':asdict(scope),
        'rules':asdict(sim.RULES[entry['league_code']]), 'runs':SEASON_SIMULATION_RUNS,
        'points':verified_points(ROOT,entry,fixtures),
        'code':{name:sha256(ROOT/'scripts'/name) for name in
                ('materialize_prepared_league_simulations.py','materialize_prepared_league_specials.py',
                 'materialize_prepared_simulations.py','simulation_priority_inputs.py')}}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def protected_digest(con):
    digest = hashlib.sha256()
    for name, schema in con.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"):
        if name in TABLES or name.startswith('sqlite_'):
            continue
        quoted = '"'+name.replace('"','""')+'"'
        digest.update(repr((name,schema)).encode())
        pk = [r[1] for r in sorted(con.execute('PRAGMA table_info('+quoted+')'),key=lambda r:r[5]) if r[5]]
        order = ','.join('"'+k.replace('"','""')+'"' for k in pk) if pk else 'rowid'
        for row in con.execute('SELECT * FROM '+quoted+' ORDER BY '+order):
            digest.update(repr(row).encode()); digest.update(b'\n')
    return digest.hexdigest()


def git(*args, check=True):
    result = subprocess.run(['git',*args],cwd=ROOT,text=True,capture_output=True)
    if check and result.returncode:
        raise RuntimeError('git '+args[0]+' failed: '+result.stderr.strip())
    return result


def validate(con, codes):
    if con.execute('PRAGMA quick_check').fetchone() != ('ok',):
        raise ValueError('Simulation database failed integrity check')
    proof = {}
    for code in codes:
        meta = con.execute('SELECT current_completed_matches,simulation_runs FROM prepared_league_simulation_meta WHERE league_code=?',(code,)).fetchone()
        rows = con.execute('SELECT team_name,current_position,current_played,current_points,title_probability,position_probabilities_json FROM prepared_league_team_simulations WHERE league_code=?',(code,)).fetchall()
        if not meta or meta[1] != SEASON_SIMULATION_RUNS or not rows or abs(sum(r[4] for r in rows)-1)>1e-8:
            raise ValueError('Invalid league simulation probability mass: '+code)
        if sorted(r[1] for r in rows) != list(range(1,len(rows)+1)):
            raise ValueError('Invalid current positions: '+code)
        for row in rows:
            probabilities = json.loads(row[5])
            if (len(probabilities)!=len(rows) or abs(sum(probabilities)-1)>1e-8
                    or any(not 0 <= p <= 1 for p in probabilities)):
                raise ValueError('Invalid team position probability mass: '+code)
        proof[code] = {'completed':meta[0], 'runs':meta[1], 'teams':len(rows),
                       'currentTop':[[r[0],r[1],r[2],r[3]] for r in sorted(rows,key=lambda r:r[1])[:4]]}
    return proof


def main():
    before = json.loads((ROOT/MANIFEST).read_text())
    base_artifact = artifact(before)
    bundle = ROOT/base_artifact['path']
    if sha256(bundle) != base_artifact['sha256']:
        raise ValueError('Published simulation bundle SHA mismatch')
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        with zipfile.ZipFile(bundle) as archive:
            archive.extract('databases/statmaker_prepared_betting.db',temp)
        db = temp/'databases/statmaker_prepared_betting.db'
        entries = sim.current_entries(json.loads(sim.INDEX_PATH.read_text()))
        with sqlite3.connect(db) as con:
            fingerprints = {e['league_code']:fingerprint(e,con) for e in entries}
            result_fingerprints = {e['league_code']:fingerprint(e,con,True) for e in entries}
            protected = protected_digest(con)
        old = before.get('metadata',{}).get(FP_KEY,{}) if before.get('metadata',{}).get(SHA_KEY)==base_artifact['sha256'] else {}
        changed = sorted(code for code, fp in fingerprints.items() if old.get(code)!=fp)
        if not changed:
            print('DOMESTIC_SIMULATION_CURRENT leagues='+str(len(fingerprints)),flush=True)
            return 0
        print('DOMESTIC_SIMULATION_REFRESH codes='+','.join(changed)+' runs='+str(SEASON_SIMULATION_RUNS),flush=True)
        command = [sys.executable,str(ROOT/'scripts/hotpatch_app_ready_simulation.py'),'--domestic-season-only']
        for code in changed:
            command += ['--league-code',code]
        subprocess.run(command,cwd=ROOT,check=True)
        generated = json.loads((ROOT/MANIFEST).read_text())
        new_artifact = artifact(generated)
        saved_bundle = temp/'verified_bundle.zip'
        shutil.copy2(ROOT/new_artifact['path'],saved_bundle)
        with zipfile.ZipFile(saved_bundle) as archive:
            archive.extract('databases/statmaker_prepared_betting.db',temp/'verified')
        with sqlite3.connect(temp/'verified/databases/statmaker_prepared_betting.db') as con:
            if protected_digest(con)!=protected:
                raise ValueError('Domestic refresh modified protected match, UEFA or statistics tables')
            proof = validate(con,changed)
        for attempt in range(1,5):
            git('fetch','--no-tags','origin','+main:refs/remotes/origin/main')
            git('reset','--hard','refs/remotes/origin/main')
            latest = json.loads((ROOT/MANIFEST).read_text())
            if artifact(latest)['sha256'] != base_artifact['sha256']:
                print('DOMESTIC_SIMULATION_SUPERSEDED newer verified bundle retained',flush=True)
                return 0
            with sqlite3.connect(db) as con:
                if any(fingerprint(e,con,True)!=result_fingerprints[e['league_code']] for e in entries):
                    print('DOMESTIC_SIMULATION_SUPERSEDED newer source results retained; next wakeup will retry',flush=True)
                    return 0
            previous = temp/'previous_manifest.json'
            previous.write_text(json.dumps(latest))
            shutil.copy2(saved_bundle,ROOT/new_artifact['path'])
            latest['artifacts'] = [new_artifact if a['id']=='app_ready_betting_bundle' else a for a in latest['artifacts']]
            now = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
            metadata = latest.setdefault('metadata',{})
            for key in ('preparedLeagueSimulationContract','preparedLeagueSimulationCount',
                        'preparedLeagueSimulationTeamCount','preparedLeagueSimulationRuns'):
                metadata[key] = generated['metadata'][key]
            metadata.update({FP_KEY:fingerprints,SHA_KEY:new_artifact['sha256'],
                'preparedDomesticLeagueSimulationGeneratedAt':now,'preparedLeagueSimulationRuns':SEASON_SIMULATION_RUNS})
            latest['generatedAt'] = now
            latest['contentVersion'] = hashlib.sha256((latest['contentVersion']+'|'+new_artifact['sha256']).encode()).hexdigest()
            (ROOT/MANIFEST).write_text(json.dumps(latest,ensure_ascii=False,indent=2)+'\n')
            report = {'status':'refreshed','generatedAt':now,'apiRequestsUsed':0,'protectedTablesUnchanged':True,
                'protectedDigest':protected,'updatedLeagues':changed,'verified':proof,'bundleSha256':new_artifact['sha256']}
            (ROOT/REPORT).parent.mkdir(parents=True,exist_ok=True)
            (ROOT/REPORT).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            subprocess.run([sys.executable,str(ROOT/'scripts/prune_app_ready_unreferenced_bundles.py'),
                            '--previous-manifest',str(previous)],cwd=ROOT,check=True)
            git('add','-A','data/statmaker/app_ready',REPORT)
            if git('diff','--cached','--quiet',check=False).returncode==0:
                return 0
            git('commit','-m','Refresh verified domestic league simulations from cached final results [skip ci]')
            if git('push','origin','HEAD:main',check=False).returncode==0:
                print('DOMESTIC_SIMULATION_PUBLISHED '+json.dumps(report),flush=True)
                return 0
        raise RuntimeError('Unable to publish domestic simulations after bounded fresh-manifest retries')


if __name__=='__main__':
    raise SystemExit(main())
