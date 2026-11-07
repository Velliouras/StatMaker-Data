from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import api_football_fetch_fixture_stats as stats
import refresh_first_half_corners as backfill
from expand_domestic_full_stats import expand_fixture
from build_domestic_live_july_normalized_stats import normalize_stats

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def block(team_id, name, half):
    return {'team': {'id': team_id, 'name': name},
            'statistics': [{'type': 'Corner Kicks', 'value': 8}],
            'statistics_1h': [{'type': 'Corner Kicks', 'value': half}]}


class FirstHalfCornersTest(unittest.TestCase):
    def test_identity_order_zero_and_full_time_are_separate(self):
        raw = [block(2, 'Away', 3), block(1, 'Home', 0)]
        result = stats.first_half_corners(raw, 1, 2, 'Home', 'Away', {'HC': 8, 'AC': 8})
        self.assertEqual(result, {'HC1H': 0, 'AC1H': 3})
        fixture = {'home_team_id': 1, 'away_team_id': 2, 'home_team': 'Home', 'away_team': 'Away',
                   'raw_statistics': raw, 'normalized_stats': {'HC': 8, 'AC': 8}}
        expanded, _ = expand_fixture(fixture)
        self.assertEqual(normalize_stats(expanded)['HC1H'], 0)
        self.assertEqual(expanded['normalized_stats']['HC'], 8)

    def test_absent_half_null_and_invalid_counts_remain_unknown(self):
        for value in (None, -1, 2.5, 9, float('nan'), True):
            self.assertEqual(stats.first_half_corners([block(1, 'Home', value)], 1, 2,
                             'Home', 'Away', {'HC': 8}), {})
        raw = block(1, 'Home', 3)
        del raw['statistics_1h']
        self.assertEqual(stats.first_half_corners([raw], 1, 2, 'Home', 'Away', {'HC': 8}), {})
        self.assertEqual(stats.first_half_corners([block(99, 'Other', 3)], None, None,
                         'Home', 'Away', {}), {})

    def test_backfill_is_capped_cached_and_preserves_full_time_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = 'data/api_football/fixture_stats/greece/test/2026/fixture_stats.json'
            index = {'leagues': [{'league_code': 'G1', 'app_season': '2026-2027',
                     'target_app_season': '2026-2027', 'lifecycle': 'active', 'cache_path': relative,
                     'api_football_league_id': 197, 'api_football_season': '2026'}]}
            fixtures = [{'fixture_id': i, 'date': (NOW - timedelta(days=i)).isoformat(), 'status': 'FT',
                         'home_team_id': 1, 'away_team_id': 2, 'home_team': 'Home', 'away_team': 'Away',
                         'normalized_stats': {'HC': 8, 'AC': 8, 'HxG': 1.7},
                         'raw_statistics': [block(1, 'Home', None), block(2, 'Away', None)]}
                        for i in range(1, 4)]
            stats.write_json(root / 'data/statmaker/domestic_enriched/index.json', index)
            stats.write_json(root / relative, {'league_id': 197, 'season': '2026', 'fixtures': fixtures})
            calls = []
            def fetch(key, endpoint, params, counter, maximum):
                self.assertEqual(params['half'], 'true')
                counter['count'] += 1
                calls.append(params['fixture'])
                return {'response': [block(2, 'Away', 2), block(1, 'Home', 1)]}
            result = backfill.refresh(root, NOW, maximum=2, fetch=fetch)
            self.assertEqual(result['requests'], 2)
            self.assertEqual(result['updated'], 2)
            cached = stats.load_json(root / relative, {})
            self.assertEqual(cached['fixtures'][0]['normalized_stats'],
                             {'HC': 8, 'AC': 8, 'HxG': 1.7, 'HC1H': 1, 'AC1H': 2})
            backfill.refresh(root, NOW, maximum=2, fetch=fetch)
            self.assertEqual(calls, [1, 2, 3])
            self.assertEqual(backfill.refresh(root, NOW, maximum=2, fetch=fetch)['requests'], 0)

    def test_old_frozen_seasons_and_future_finals_are_not_backfilled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = 'data/api_football/fixture_stats/test/fixture_stats.json'
            entry = {'league_code': 'G1', 'app_season': '2025-2026', 'target_app_season': '2026-2027',
                     'lifecycle': 'active', 'cache_path': relative, 'api_football_league_id': 197,
                     'api_football_season': '2025'}
            stats.write_json(root / relative, {'league_id': 197, 'season': '2025', 'fixtures': [
                {'fixture_id': 1, 'date': (NOW + timedelta(days=1)).isoformat(), 'status': 'FT'}]})
            stats.write_json(root / 'data/statmaker/domestic_enriched/index.json', {'leagues': [entry]})
            self.assertEqual(backfill.candidates(root, NOW), [])


if __name__ == '__main__':
    unittest.main()
