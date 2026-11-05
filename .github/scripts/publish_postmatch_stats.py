#!/usr/bin/env python3
"""Replay provider cache changes, then rebuild derived stats on current main."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time

SOURCES = ['data/api_football/fixture_stats/',
           'data/statmaker/monitoring/recent_finished_match_stats.json']
DERIVED = ['data/api_football/domestic_normalized_fixture_stats.json',
           'data/statmaker/domestic_enriched/', 'data/statmaker/domestic_rosters.json',
           'data/statmaker/uefa_support_history.json', 'data/statmaker/update_manifest.json',
           'reports/domestic_live_july_full_stats_expansion.json']


def git(*args, check=True):
    return subprocess.run(['git', *args], check=check, capture_output=True, text=True)


def rebuild():
    # Deliberately no provider refresh here: retries consume no API quota.
    for script in ['expand_domestic_full_stats.py']:
        subprocess.run([sys.executable, 'scripts/' + script], check=True)
    subprocess.run([sys.executable, '-c', '''
import sys
sys.path.insert(0, 'scripts')
import domestic_live_july_pipeline as pipeline
registry = pipeline.load_json(pipeline.REGISTRY_PATH, {}).get('leagues', [])
assert registry, 'Domestic Stats registry is missing'
pipeline.build_app_statistics(registry)
'''], check=True)
    for script in ['finalize_domestic_live_july_artifacts.py',
                   'build_domestic_live_july_normalized_stats.py', 'build_uefa_support_history.py']:
        subprocess.run([sys.executable, 'scripts/' + script], check=True)
    subprocess.run([sys.executable, 'scripts/build_statmaker_update_manifest.py',
                    '--profile', 'main', '--branch', 'main', '--output', DERIVED[5]], check=True)


def publish(updated, builder=rebuild, pause=time.sleep):
    if git('status', '--porcelain').stdout:
        raise RuntimeError('Publication requires a clean generated commit')
    if len(git('show', '-s', '--format=%P', 'HEAD').stdout.split()) != 1:
        raise RuntimeError('Expected one generated commit with one parent')
    changed = git('diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD').stdout.splitlines()
    if any(not any(p == x.rstrip('/') or p.startswith(x) for x in SOURCES) for p in changed):
        raise RuntimeError('Generated commit must contain only provider caches and retry state')
    # Save only the source delta, never stale derived JSON. Keep it outside the
    # worktree because rebuilding can create untracked auxiliary output.
    patch = git('diff', '--binary', 'HEAD^', 'HEAD', '--', *SOURCES).stdout
    with tempfile.NamedTemporaryFile(mode='w', suffix='.patch') as saved:
        saved.write(patch)
        saved.flush()
        for attempt in range(1, 7):
            git('fetch', '--no-tags', 'origin', '+refs/heads/main:refs/remotes/origin/main')
            git('reset', '--hard', 'refs/remotes/origin/main')
            applied = git('apply', '--3way', '--index', saved.name, check=False)
            if applied.returncode:
                raise RuntimeError('Provider cache conflict; refusing to overwrite newer source: ' + applied.stderr)
            if updated:
                builder()
            owned = SOURCES + (DERIVED if updated else [])
            for path in owned:
                if Path(path).exists():
                    git('add', '-A', '--', path)
            staged = git('diff', '--cached', '--name-only').stdout.splitlines()
            if any(not any(p == x.rstrip('/') or p.startswith(x) for x in owned) for p in staged):
                raise RuntimeError('Unexpected staged paths')
            if staged:
                git('commit', '-m', 'Publish prompt verified postmatch fixture statistics [skip ci]')
            # Discard only scratch produced in this dedicated checkout, before retry.
            git('restore', '--worktree', '.')
            git('clean', '-fd', '--', 'reports', 'data')
            pushed = git('push', 'origin', 'HEAD:refs/heads/main', check=False)
            if pushed.returncode == 0:
                print(f'POSTMATCH_PUBLISH_OK attempt={attempt}', flush=True)
                return
            # Retry only a branch race, not authentication/network failures.
            if 'fetch first' not in pushed.stderr and 'non-fast-forward' not in pushed.stderr:
                raise RuntimeError(pushed.stderr)
            pause(min(attempt * 2, 8))
    raise RuntimeError('Postmatch publication remained busy after six rebuilds')


if __name__ == '__main__':
    publish(os.environ.get('POSTMATCH_UPDATED') == 'true')
