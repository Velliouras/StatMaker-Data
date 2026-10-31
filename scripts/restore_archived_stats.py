"""Append proven historical scopes to an Android checkpoint, preserving current rows."""
from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import tempfile
from pathlib import Path
from scripts import hotpatch_app_ready_stats as stats
from scripts.reconcile_app_ready_stats_scope import db_season
from scripts.statistics_archive_catalog import statistics_catalog


def merge_archived_normalized(path: Path, values: dict) -> None:
    if not values:
        return
    with path.open('rb') as handle:
        magic, version, count = struct.unpack('>iii', handle.read(12))
        if (magic, version) != (0x534D4453, 2) or count < 0:
            raise ValueError('Unexpected normalized statistics snapshot')
        existing = {}
        for _ in range(count):
            length = struct.unpack('>H', handle.read(2))[0]
            key = handle.read(length).decode('utf-8')
            numbers = []
            for _ in stats.NORMALIZED_FIELDS:
                flag = handle.read(1)
                if flag not in (b'\0', b'\1'):
                    raise ValueError('Invalid normalized metric flag')
                numbers.append(struct.unpack('>d', handle.read(8))[0] if flag == b'\1' else None)
            if key in existing:
                raise ValueError('Duplicate normalized key')
            existing[key] = tuple(numbers)
        if handle.read(1):
            raise ValueError('Trailing normalized snapshot bytes')
    existing.update(values)
    pending = path.with_suffix('.restoring')
    with pending.open('wb') as handle:
        handle.write(struct.pack('>iii', magic, version, len(existing)))
        for key, numbers in existing.items():
            stats.write_utf(handle, key)
            for number in numbers:
                handle.write(b'\1' if number is not None else b'\0')
                if number is not None:
                    handle.write(struct.pack('>d', number))
    pending.replace(path)


def restore(database: Path, normalized: Path | None = None, root: Path = stats.ROOT) -> int:
    catalogue = statistics_catalog(root, json.loads((root / 'data/statmaker/domestic_enriched/index.json').read_text()))
    scopes = [(db_season(row['app_season']), row['league_code'])
              for row in catalogue['leagues'] if row.get('statistics_archived')]
    restored = 0
    with tempfile.TemporaryDirectory() as directory:
        canonical = Path(directory) / 'canonical.db'
        stats.create_stats_db(canonical, repository_root=root)
        connection = sqlite3.connect(database)
        try:
            connection.execute('ATTACH DATABASE ? AS canonical', (str(canonical),))
            columns = [row[1] for row in connection.execute('PRAGMA canonical.table_info(matches)') if row[1] != 'id']
            column_sql = ','.join('"' + c + '"' for c in columns)
            before = connection.execute('SELECT * FROM matches ORDER BY match_key').fetchall()
            target_columns = [row[1] for row in connection.execute('PRAGMA table_info(matches)')]
            key_index, season_index, code_index = [target_columns.index(c) for c in ('match_key','season','division')]
            unrelated = [row for row in before if (row[season_index],row[code_index]) not in scopes]
            connection.execute('BEGIN IMMEDIATE')
            for season, code in scopes:
                existing = {r[0] for r in connection.execute('SELECT match_key FROM matches WHERE season=? AND division=?', (season,code))}
                expected = {r[0] for r in connection.execute('SELECT match_key FROM canonical.matches WHERE season=? AND division=?', (season,code))}
                if not expected or existing - expected:
                    raise ValueError(f'Unverified existing archive rows: {code}@{season}')
                connection.execute(f'INSERT OR REPLACE INTO matches ({column_sql}) SELECT {column_sql} FROM canonical.matches WHERE season=? AND division=?', (season,code))
                restored += len(expected - existing)
            after = connection.execute('SELECT * FROM matches ORDER BY match_key').fetchall()
            if [row for row in after if (row[season_index],row[code_index]) not in scopes] != unrelated:
                raise ValueError('Historical restoration altered current statistics')
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Invalid restored statistics database')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    if normalized is not None:
        merge_archived_normalized(normalized, stats.archived_normalized_values(repository_root=root))
    print('ARCHIVED_STATS_RESTORED', f'scopes={len(scopes)}', f'newMatches={restored}', 'currentRowsPreserved=true')
    return restored


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--normalized', type=Path)
    args = parser.parse_args()
    restore(args.database, args.normalized)
