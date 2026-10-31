"""Fill the 5/10-minute gaps between external watchdog wakeups; never call the provider."""
from datetime import datetime, timedelta, timezone
import base64
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen

LIVE = {'1H', 'HT', '2H', 'ET', 'BT', 'P'}
WORKFLOW = 'priority-results-standings.yml'


def instant(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def active(document, now):
    for fixture in document.get('fixtures', []):
        kickoff = instant(fixture.get('date_utc'))
        if not kickoff:
            continue
        if fixture.get('status') in LIVE and now-timedelta(hours=6) <= kickoff <= now:
            return True
        if fixture.get('status') in {'NS', 'TBD'} and now-timedelta(minutes=15) <= kickoff <= now+timedelta(minutes=15):
            return True
    return False


class GitHub:
    def __init__(self):
        self.root = 'https://api.github.com/repos/' + os.environ['GITHUB_REPOSITORY'] + '/'
        self.token = os.environ['GITHUB_TOKEN']

    def request(self, path, body=None):
        request = Request(self.root + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.token,
                     'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json',
                     'X-GitHub-Api-Version': '2022-11-28',
                     'User-Agent': 'StatMaker-Live-Cadence'})
        with urlopen(request, timeout=30) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError('Oversized GitHub response')
        value = json.loads(raw) if raw else {}
        if value.get('encoding') == 'base64' and 'content' in value:
            return json.loads(base64.b64decode(value['content']))
        return value

    def document(self, code):
        return self.request('contents/data/statmaker/priority_refresh/' + code + '.json?ref=main')

    def busy(self):
        runs = self.request('actions/workflows/' + WORKFLOW + '/runs?branch=main&per_page=5')
        return any(r.get('status') in {'queued', 'in_progress', 'waiting', 'pending'}
                   for r in runs.get('workflow_runs', []))

    def dispatch(self):
        self.request('actions/workflows/' + WORKFLOW + '/dispatches', {'ref': 'main'})


def probe(models, specs, github, now):
    candidates = [code for code, doc in models.items()
                  if specs.get(code, {}).get('tier') in {'primary', 'europe', 'background'} and active(doc, now)]
    overdue = []
    live_count = 0
    for code in candidates:
        document = github.document(code)
        if (document.get('contract') != 'statmaker-league-tables-v1' or
                document.get('leagueCode') != code or document.get('season') != specs[code]['season']):
            raise ValueError('Invalid live read model: ' + code)
        models[code] = document
        if not active(document, now):
            continue
        live_count += 1
        checked = instant(document.get('resultsCheckedAtUTC'))
        if checked is None or int(checked.timestamp())//300 < int(now.timestamp())//300:
            overdue.append(code)
    if not overdue:
        return 'fresh' if live_count else 'inactive'
    if github.busy():
        return 'already-running'
    github.dispatch()
    return 'dispatched:' + ','.join(sorted(overdue))


def next_slot(now):
    return (int(now.timestamp())//300+1)*300 + 20


def main():
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root/'config/api_football_refresh_policy.json').read_text())
    specs = {row['code']: row for row in policy['leagueIds'].values()}
    models = {p.stem: json.loads(p.read_text()) for p in
              (root/'data/statmaker/priority_refresh').glob('*.json') if p.stem != 'index'}
    github = GitHub()
    for slot in range(3):
        now = datetime.now(timezone.utc)
        result = probe(models, specs, github, now)
        print('LIVE_CADENCE', now.isoformat(), result, flush=True)
        if result == 'inactive' or slot == 2:
            break
        target = next_slot(now)
        while time.time() < target:
            # Bounded, interruptible waits on the public runner; no provider requests.
            time.sleep(max(0, min(30, target-time.time())))


if __name__ == '__main__':
    main()
