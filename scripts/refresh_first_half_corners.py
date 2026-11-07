"""Backfill verified first-half corner counts within the shared API request cap."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import api_football_fetch_fixture_stats as stats

try:
    from api_football_budget import BudgetStop, priority
except ImportError:
    from api_budget_runtime.api_football_budget import BudgetStop, priority

ROOT = Path(__file__).resolve().parents[1]
PRIMARY = {"G1": 0, "E0": 1, "SP1": 2, "I1": 3, "D1": 4, "F1": 5}


def candidates(root, now, codes=None):
    index = stats.load_json(root / 'data/statmaker/domestic_enriched/index.json', {})
    pending = []
    for entry in index.get('leagues', []):
        code = entry.get('league_code')
        if codes and code not in codes:
            continue
        if (entry.get('lifecycle') != 'active'
                or entry.get('app_season') != entry.get('target_app_season')):
            continue
        relative = Path(str(entry.get('cache_path') or ''))
        if not str(relative).startswith('data/api_football/fixture_stats/') or '..' in relative.parts:
            continue
        path = root / relative
        cache = stats.load_json(path, {})
        if (str(cache.get('league_id')) != str(entry.get('api_football_league_id'))
                or str(cache.get('season')) != str(entry.get('api_football_season'))):
            continue
        for fixture in cache.get('fixtures', []):
            if fixture.get('status') not in {'FT', 'AET', 'PEN'}:
                continue
            normalized = fixture.get('normalized_stats') or {}
            if all(normalized.get(k) is not None for k in ('HC1H', 'AC1H')):
                continue
            try:
                date = datetime.fromisoformat(str(fixture.get('date') or '').replace('Z', '+00:00'))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                if date > now or date.year < 2024:
                    continue
                attempted = fixture.get('firstHalfCornersAttemptedAtUTC')
                if attempted and now - datetime.fromisoformat(attempted.replace('Z', '+00:00')) < timedelta(days=14):
                    continue
            except (TypeError, ValueError):
                continue
            if not isinstance(fixture.get('fixture_id'), int):
                continue
            pending.append((PRIMARY.get(code, 6), -date.timestamp(), code, path, cache, fixture))
    return sorted(pending, key=lambda row: (row[0], row[1], row[2], row[-1]['fixture_id']))


def refresh(root=ROOT, now=None, maximum=12, codes=None, fetch=None):
    if not 1 <= maximum <= 500:
        raise ValueError('First-half corner cap must be 1..500')
    now = now or datetime.now(timezone.utc)
    selected = candidates(root, now, codes)
    counter = {'count': 0}
    touched = {}
    updated = 0
    key = os.getenv('API_FOOTBALL_KEY', '').strip()
    fetch = fetch or stats.api_get
    if selected and fetch is stats.api_get and not key:
        raise RuntimeError('API_FOOTBALL_KEY missing')
    for _, _, code, path, cache, fixture in selected:
        if counter['count'] >= maximum:
            break
        try:
            with priority('background'):
                response = fetch(key, 'fixtures/statistics',
                                 {'fixture': fixture['fixture_id'], 'half': 'true'}, counter, maximum)
        except (BudgetStop, stats.RequestLimitReached):
            break
        except (OSError, ValueError, RuntimeError) as error:
            print('FIRST_HALF_CORNERS_RETRY', code, fixture['fixture_id'], type(error).__name__)
            continue
        if stats.api_errors(response):
            continue
        raw = stats.response_items(response)
        half = stats.first_half_corners(raw, fixture.get('home_team_id'), fixture.get('away_team_id'),
                                       fixture.get('home_team'), fixture.get('away_team'),
                                       fixture.get('normalized_stats') or {})
        fixture['firstHalfCornersAttemptedAtUTC'] = now.isoformat()
        if half:
            fixture.setdefault('normalized_stats', {}).update(half)
            # Preserve the full original provider response and merge only 1H arrays
            # by team identity, so absent FT values cannot erase existing statistics.
            previous = fixture.get('raw_statistics') or []
            for block in raw:
                team = block.get('team') or {}
                def same_team(item):
                    old = item.get('team') or {}
                    return ((team.get('id') is not None and old.get('id') == team['id'])
                            or (team.get('name') and str(old.get('name') or '').casefold() == str(team['name']).casefold()))
                existing = next((item for item in previous if same_team(item)), None)
                if existing is not None:
                    existing['statistics_1h'] = block.get('statistics_1h') or []
                elif block.get('statistics_1h'):
                    previous.append({'team': team, 'statistics': [], 'statistics_1h': block['statistics_1h']})
            fixture['raw_statistics'] = previous
            updated += 1
        touched[path] = cache
    for path, cache in touched.items():
        stats.write_json(path, cache)
    result = {'requests': counter['count'], 'updated': updated, 'stateChanged': bool(touched)}
    print('FIRST_HALF_CORNERS_REFRESH', json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-requests', type=int, default=120)
    parser.add_argument('--league-codes', default='ALL')
    args = parser.parse_args()
    codes = None if args.league_codes == 'ALL' else set(args.league_codes.split(','))
    refresh(maximum=args.max_requests, codes=codes)


if __name__ == '__main__':
    main()
