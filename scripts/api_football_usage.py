#!/usr/bin/env python3
"""Record provider quota snapshots without retaining account details or credentials."""
import argparse
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENDPOINT = 'https://v3.football.api-sports.io/status'
FIELDS = ('dateUtc', 'used', 'limit', 'remaining', 'usedPercent', 'reserve',
          'availableAboveReserve', 'observedAtUtc', 'source', 'runUrl')


def count(value):
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError('Invalid provider quota counter')
    return int(value)


def snapshot(used, limit, observed, reserve=1500, source='api-status', run_url=''):
    used, limit, reserve = count(used), count(limit), count(reserve)
    if limit <= 0 or used > limit or observed.tzinfo is None:
        raise ValueError('Invalid quota measurement')
    stamp = observed.astimezone(timezone.utc)
    return dict(dateUtc=stamp.date().isoformat(), used=used, limit=limit,
                remaining=limit-used, usedPercent=round(100*used/limit, 4), reserve=reserve,
                availableAboveReserve=max(0, limit-used-reserve),
                observedAtUtc=stamp.isoformat().replace('+00:00', 'Z'),
                source=source, runUrl=run_url)


def update(history, payload, observed, reserve=1500, run_url=''):
    if payload.get('errors'):
        raise ValueError('Provider status returned an error')
    requests = payload.get('response', {}).get('requests', {})
    current = snapshot(requests.get('current'), requests.get('limit_day'), observed,
                       reserve, run_url=run_url)
    days = {}
    for row in history.get('days', []):
        old = snapshot(row['used'], row['limit'],
                       datetime.fromisoformat(row['observedAtUtc'].replace('Z', '+00:00')),
                       row.get('reserve', reserve), row.get('source', 'workflow-log'),
                       row.get('runUrl', ''))
        previous = days.get(old['dateUtc'])
        if previous is None or old['observedAtUtc'] > previous['observedAtUtc']:
            days[old['dateUtc']] = old
    previous = days.get(current['dateUtc'])
    if previous is None or current['observedAtUtc'] >= previous['observedAtUtc']:
        days[current['dateUtc']] = current
    return dict(schemaVersion=1, provider='API-Football', timezone='UTC',
                measurement='latest-observed-counter-not-guaranteed-end-of-day-total',
                days=[days[day] for day in sorted(days)])


def csv_text(history):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    writer.writerows({key: row.get(key, '') for key in FIELDS} for row in history['days'])
    return stream.getvalue()


def fetch_status(key, opener=None, pause=None):
    opener, pause = opener or urlopen, pause or time.sleep
    request = Request(ENDPOINT, headers={'x-apisports-key': key, 'Accept': 'application/json'},
                      method='GET')
    for attempt in range(3):
        try:
            with opener(request, timeout=20) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError('API-Football status HTTP ' + str(exc.code)) from None
        except (URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError('API-Football status temporarily unavailable') from None
        pause(2*(attempt+1))


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', type=Path, default=Path('data/statmaker/monitoring/api_football_usage.json'))
    parser.add_argument('--csv', type=Path, default=Path('data/statmaker/monitoring/api_football_usage.csv'))
    parser.add_argument('--reserve', type=int, default=1500)
    parser.add_argument('--run-url', default='')
    args = parser.parse_args()
    key = os.environ.get('API_FOOTBALL_KEY', '').strip()
    if not key:
        raise RuntimeError('API_FOOTBALL_KEY is missing')
    history = json.loads(args.json.read_text()) if args.json.exists() else {'days': []}
    payload = fetch_status(key)
    observed = datetime.now(timezone.utc)
    history = update(history, payload, observed, args.reserve, args.run_url)
    write(args.json, json.dumps(history, ensure_ascii=False, indent=2)+'\n')
    write(args.csv, csv_text(history))
    print('API_FOOTBALL_USAGE_SNAPSHOT '+json.dumps(history['days'][-1]))


if __name__ == '__main__':
    main()
