"""Publish fetched cache changes on fresh main, then rebuild derived data offline.

Do not rebase generated league JSON. Merge only our source delta into the latest
cache and regenerate every derived artifact; concurrent verified values win when
both writers changed the same field. Provider requests are never repeated here.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SOURCES = ('data/api_football/fixture_stats/', 'data/statmaker/domestic_live_july_registry.json',
           'data/statmaker/domestic_rosters.json', 'data/statmaker/domestic_historical_stats_freeze.json')
REPORTS = ('reports/api_football_fixture_stats_fetch.', 'reports/domestic_live_july_stats_refresh.json',
           'reports/domestic_targeted_stats_backfill.log')
OUTPUTS = ('data/api_football/fixture_stats', 'data/api_football/domestic_normalized_fixture_stats.json',
           'data/statmaker/domestic_live_july_registry.json', 'data/statmaker/domestic_rosters.json',
           'data/statmaker/domestic_enriched', 'data/statmaker/uefa_support_history.json',
           'data/statmaker/update_manifest.json', 'reports')


def git(*args, check=True):
    result = subprocess.run(['git', *args], text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError('git '+args[0]+' failed: '+result.stderr.strip())
    return result


def identity(row):
    if not isinstance(row, dict):
        return None
    if row.get('fixture_id'):
        return ('fixture', str(row['fixture_id']))
    code = row.get('league_code') or row.get('leagueCode')
    if code:
        return ('league', str(code), str(row.get('app_season') or row.get('season') or ''),
                str(row.get('cache_path') or ''))
    if row.get('id'):
        return ('id', str(row['id']))
    if isinstance(row.get('team'), dict) and row['team'].get('id'):
        return ('team', str(row['team']['id']))
    if row.get('type'):
        return ('statistic', str(row['type']))
    return None


def merge_delta(base, current, incoming):
    if incoming == base or current == incoming:
        return deepcopy(current)
    if current == base:
        return deepcopy(incoming)
    if isinstance(incoming, dict) and isinstance(current, dict):
        old = base if isinstance(base, dict) else {}
        merged = deepcopy(current)
        for key, value in incoming.items():
            if key in old and value == old[key]:
                continue
            merged[key] = merge_delta(old.get(key), current.get(key), value)
        return merged
    if isinstance(incoming, list) and isinstance(current, list):
        keys = [identity(r) for r in current+incoming]
        if all(key is not None for key in keys):
            old = {identity(r): r for r in base or []}
            latest = {identity(r): deepcopy(r) for r in current}
            for row in incoming:
                key = identity(row)
                latest[key] = merge_delta(old.get(key), latest.get(key), row)
            return list(latest.values())
        # Unkeyed metadata may only add records; never erase concurrent values.
        result = deepcopy(current)
        for row in incoming:
            if row not in (base or []) and row not in result:
                result.append(deepcopy(row))
        return result
    # Two different edits to the same field: retain the newer published value.
    return deepcopy(current)


def read_at(ref, name):
    result = git('show', ref+':'+name, check=False)
    return json.loads(result.stdout) if result.returncode == 0 else None


def rebuild():
    env = dict(os.environ)
    env.pop('API_FOOTBALL_KEY', None)
    def run(*args):
        subprocess.run([sys.executable, *args], env=env, check=True)
    run('scripts/validate_domestic_cache_provider_identity.py')
    run('scripts/expand_domestic_full_stats.py')
    run('-c', "import sys;sys.path.insert(0,'scripts');import domestic_live_july_pipeline as p;"
        "rows=p.load_json(p.REGISTRY_PATH,{}).get('leagues',[]);"
        "assert rows,'Domestic registry is empty';p.build_app_statistics(rows)")
    run('scripts/finalize_domestic_live_july_artifacts.py')
    run('scripts/build_domestic_live_july_normalized_stats.py')
    run('scripts/build_uefa_support_history.py')
    run('scripts/build_statmaker_update_manifest.py', '--profile', 'main', '--branch', 'main',
        '--output', 'data/statmaker/update_manifest.json')


def publish(attempts=4, rebuild_outputs=rebuild, pause=time.sleep):
    base_ref = git('rev-parse', 'HEAD').stdout.strip()
    names = set(git('diff', '--name-only', 'HEAD').stdout.splitlines())
    names.update(git('ls-files', '--others', '--exclude-standard').stdout.splitlines())
    sources, reports = {}, {}
    for name in sorted(names):
        path = Path(name)
        if not path.is_file():
            continue
        if name.startswith(SOURCES):
            sources[name] = (read_at(base_ref, name), json.loads(path.read_text()))
        elif name.startswith(REPORTS):
            reports[name] = path.read_bytes()
    if not sources:
        print('TARGETED_BACKFILL_SOURCE_ALREADY_CURRENT', flush=True)
        return
    for attempt in range(1, attempts+1):
        git('fetch', '--no-tags', 'origin', '+main:refs/remotes/origin/main')
        upstream = git('rev-parse', 'refs/remotes/origin/main').stdout.strip()
        git('reset', '--hard', upstream)
        for name, (base, incoming) in sources.items():
            current = read_at(upstream, name)
            merged = merge_delta(base, current, incoming)
            path = Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(merged, ensure_ascii=False, indent=2)+'\n')
        for name, content in reports.items():
            Path(name).parent.mkdir(parents=True, exist_ok=True)
            Path(name).write_bytes(content)
        rebuild_outputs()
        # Stage only data products that this offline rebuild actually owns.
        changed = set(git('diff', '--name-only', 'HEAD').stdout.splitlines())
        changed.update(git('ls-files', '--others', '--exclude-standard').stdout.splitlines())
        owned = [p for p in sorted(changed) if p.startswith(OUTPUTS) or p.startswith(SOURCES)]
        if owned:
            git('add', '--', *owned)
        if git('diff', '--cached', '--quiet', check=False).returncode == 0:
            print('TARGETED_BACKFILL_ALREADY_PUBLISHED', flush=True)
            return
        git('commit', '-m', 'Publish targeted statistics from merged verified caches [skip ci]')
        pushed = git('push', 'origin', 'HEAD:main', check=False)
        if pushed.returncode == 0:
            print(f'TARGETED_BACKFILL_PUBLISH_OK attempt={attempt} sourceFiles={len(sources)}', flush=True)
            return
        print(f'TARGETED_BACKFILL_PUBLISH_RETRY attempt={attempt}', flush=True)
        pause(min(attempt*2, 8))
    raise RuntimeError('Targeted cache publication failed after bounded fresh-tree retries')


if __name__ == '__main__':
    publish()
