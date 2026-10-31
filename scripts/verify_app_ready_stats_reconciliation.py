#!/usr/bin/env python3
"""Verify reconciliation on a temporary copy of the real published stats DB."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reconcile_app_ready_stats_scope import db_season, reconcile
from scripts.validate_app_ready_archived_stats import normalize_code, validated_archived_scope_counts


def verify(root: Path, league_code='F1', app_season='2025-2026', full_scope=False) -> None:
    root = root.resolve()
    manifest = json.loads((root / 'data/statmaker/app_ready/update_manifest.json').read_text())
    records = [row for row in manifest['artifacts'] if row['id'] == 'app_ready_stats_bundle']
    if len(records) != 1:
        raise ValueError('Expected exactly one published stats bundle')
    record = records[0]
    bundle = (root / record['path']).resolve()
    if not bundle.is_relative_to(root / 'data/statmaker/app_ready'):
        raise ValueError('Unsafe published bundle path')
    raw = bundle.read_bytes()
    if len(raw) != record['bytes'] or hashlib.sha256(raw).hexdigest() != record['sha256']:
        raise ValueError('Published stats bundle hash/size mismatch')
    season = db_season(app_season)
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / 'statmaker.db'
        with zipfile.ZipFile(bundle) as archive:
            database.write_bytes(archive.read('databases/statmaker.db'))
        def other_rows():
            with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
                return connection.execute(
                    'SELECT * FROM matches WHERE NOT (season=? AND division=?) ORDER BY match_key',
                    (season, league_code),
                ).fetchall()
        before = other_rows()
        removed, remaining = reconcile(database, root / 'data/statmaker/domestic_enriched/index.json',
                                       root, league_code, app_season)
        if other_rows() != before:
            raise ValueError('Reconciliation changed unrelated leagues or seasons')
        if bundle.read_bytes() != raw:
            raise ValueError('Published bundle was modified')
        if full_scope:
            index = json.loads((root / 'data/statmaker/domestic_enriched/index.json').read_text())
            expected = {(db_season(row['app_season']), normalize_code(row['league_code'])): int(row['completed_fixtures'])
                        for row in index['leagues'] if int(row.get('completed_fixtures') or 0) > 0}
            with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
                actual = {(str(season), normalize_code(code)): int(count)
                          for season, code, count in connection.execute(
                              'SELECT season,division,COUNT(*) FROM matches GROUP BY season,division')}
            expected.update(validated_archived_scope_counts(database, index, root, actual, expected))
            if actual != expected:
                raise ValueError('Published stats scope/count mismatch: ' + repr({
                    'missing': sorted(set(expected) - set(actual)), 'extra': sorted(set(actual) - set(expected)),
                    'counts': [(scope, count, actual.get(scope)) for scope, count in expected.items() if actual.get(scope) != count],
                }))
            print('APP_READY_PUBLISHED_STATS_FULL_SCOPE_OK', f'scopes={len(expected)}',
                  f'matches={sum(expected.values())}', flush=True)
        print('APP_READY_PUBLISHED_STATS_RECONCILIATION_OK', f'scope={league_code}@{season}',
              f'removed={removed}', f'remaining={remaining}', f'unrelatedRowsPreserved={len(before)}',
              'publishedBundleUnchanged=true', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, default=Path('.'))
    parser.add_argument('--full-scope', action='store_true')
    args = parser.parse_args()
    verify(args.repo_root, full_scope=args.full_scope)
