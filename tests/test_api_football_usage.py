import csv
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import api_football_usage as usage


def payload(used=772, limit=7500):
    return {'errors': [], 'response': {'requests': {'current': used, 'limit_day': limit},
            'account': {'email': 'private@example.com', 'apiKey': 'private-key'}}}


class ApiUsageTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat('2026-10-25T17:55:46+00:00')

    def test_dashboard_numbers_and_preserved_operational_reserve(self):
        row = usage.update({'days': []}, payload(), self.now)['days'][0]
        self.assertEqual((row['used'], row['remaining'], row['availableAboveReserve']), (772, 6728, 5228))
        self.assertAlmostEqual(row['usedPercent'], 10.2933)

    def test_new_day_is_an_independent_counter_not_a_sum_or_carryover(self):
        history = usage.update({'days': []}, payload(), self.now)
        result = usage.update(history, payload(5), datetime.fromisoformat('2026-10-26T00:08:00+00:00'))
        self.assertEqual([row['used'] for row in result['days']], [772, 5])

    def test_snapshots_replace_the_counter_instead_of_adding_requests_twice(self):
        history = usage.update({'days': []}, payload(), self.now)
        result = usage.update(history, payload(800), datetime.fromisoformat('2026-10-25T18:15:00+00:00'))
        self.assertEqual(len(result['days']), 1)
        self.assertEqual(result['days'][0]['used'], 800)

    def test_utc_day_is_used_even_after_local_midnight(self):
        row = usage.update({'days': []}, payload(), datetime.fromisoformat('2026-10-26T01:00:00+02:00'))['days'][0]
        self.assertEqual(row['dateUtc'], '2026-10-25')

    def test_account_details_are_never_written_to_json_or_csv(self):
        result = usage.update({'days': [], 'email': 'private@example.com'}, payload(), self.now)
        output = json.dumps(result)+usage.csv_text(result)
        self.assertNotIn('private@example.com', output)
        self.assertNotIn('private-key', output)
        self.assertNotIn('account', output)

    def test_invalid_or_error_responses_do_not_become_zero_usage(self):
        for value in (payload(-1), payload(7501), payload(True), payload(1, 0),
                      {'errors': {'token': 'invalid'}}, {'response': {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                usage.update({'days': []}, value, self.now)

    def test_old_observation_cannot_overwrite_a_newer_counter(self):
        history = usage.update({'days': []}, payload(800), self.now)
        result = usage.update(history, payload(700), datetime.fromisoformat('2026-10-25T17:00:00+00:00'))
        self.assertEqual(result['days'][0]['used'], 800)

    def test_csv_keeps_dates_timestamps_and_uses_only_observed_days(self):
        history = usage.update({'days': []}, payload(), self.now)
        rows = list(csv.DictReader(io.StringIO(usage.csv_text(history))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['dateUtc'], '2026-10-25')
        self.assertEqual(rows[0]['observedAtUtc'], '2026-10-25T17:55:46Z')

    def test_only_status_is_requested_and_retries_are_bounded(self):
        calls, pauses = [], []
        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) < 3:
                raise HTTPError(request.full_url, 503, 'unavailable', {}, None)
            return io.BytesIO(json.dumps(payload()).encode())
        self.assertEqual(usage.fetch_status('test-key', opener, pauses.append), payload())
        self.assertEqual(calls, [usage.ENDPOINT]*3)
        self.assertEqual(pauses, [2, 4])


if __name__ == '__main__':
    unittest.main()
