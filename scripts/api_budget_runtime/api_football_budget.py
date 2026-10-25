"""Atomic, shared API-Football allocations for every provider request in Actions."""
import atexit
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request

HOST = 'v3.football.api-sports.io'
BRANCH = 'api-football-budget'
STATE_PATH = 'data/statmaker/monitoring/api_football_budget_state.json'
BLOCK = 8
TOTAL_STOP = 6000
BACKGROUND_STOP = 4500
RESERVE = 1500
_tier = ContextVar('api_football_request_priority', default=None)
_original_open = None
_runtime = None
_lock = threading.RLock()


class BudgetStop(SystemExit):
    def __init__(self, reason):
        self.reason = reason
        print('API_FOOTBALL_BUDGET_STOP '+reason, flush=True)
        super().__init__(0)


@contextmanager
def priority(tier):
    if tier not in ('primary', 'europe', 'background'):
        raise ValueError('Invalid API budget priority')
    token = _tier.set(tier)
    try:
        yield
    finally:
        _tier.reset(token)


def utcnow():
    return datetime.now(timezone.utc)


def allocate(state, day, used, limit, tier, lease_id, owner, closing=None, block=BLOCK):
    """Pure compare-and-swap transition. Outstanding blocks always count in full."""
    if not isinstance(used, int) or isinstance(used, bool) or not 0 <= used <= limit:
        raise ValueError('Unknown provider quota')
    if not state or state.get('dayUtc') != day:
        state = dict(schemaVersion=1, dayUtc=day, committed=used, leases={})
    else:
        state = copy.deepcopy(state)
        if (state.get('schemaVersion') != 1 or not isinstance(state.get('leases'), dict)
                or not isinstance(state.get('committed'), int) or state['committed'] < 0
                or any(not isinstance(v, dict) or not isinstance(v.get('reserved'), int)
                       or not 1 <= v['reserved'] <= BLOCK for v in state['leases'].values())):
            raise ValueError('Invalid shared budget state')
    if closing:
        previous = state['leases'].pop(closing[0], None)
        if previous:
            consumed = closing[1]
            if not 0 <= consumed <= previous['reserved']:
                raise ValueError('Invalid consumed allocation')
            state['committed'] += consumed
    threshold = min(TOTAL_STOP, limit-RESERVE,
                    BACKGROUND_STOP if tier == 'background' else TOTAL_STOP)
    estimated = max(state['committed'], used) + sum(x['reserved'] for x in state['leases'].values())
    amount = min(block, max(0, threshold-estimated))
    if amount == 0:
        raise BudgetStop('priority='+tier+' estimated='+str(estimated)+' stop='+str(threshold))
    state['leases'][lease_id] = dict(reserved=amount, owner=owner, priority=tier)
    state.update(observedUsed=used, limit=limit, estimatedAllocated=estimated+amount,
                 totalStop=TOTAL_STOP, backgroundStop=BACKGROUND_STOP, reserve=RESERVE)
    return state, amount


class GithubStore:
    def __init__(self, opener):
        self.opener = opener
        self.token = os.getenv('STATMAKER_API_BUDGET_TOKEN') or os.getenv('GITHUB_TOKEN')
        self.repo = os.getenv('GITHUB_REPOSITORY', 'Velliouras/StatMaker-Data')
        if not self.token:
            raise ValueError('Missing shared-budget credential')
        api = os.getenv('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
        self.url = api+'/repos/'+self.repo+'/contents/'+STATE_PATH

    def headers(self):
        return {'Authorization': 'Bearer '+self.token, 'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'StatMaker-API-Budget'}

    def read(self):
        try:
            with self.opener(Request(self.url+'?'+urlencode({'ref': BRANCH}), headers=self.headers()), timeout=20) as r:
                doc = json.loads(r.read())
            return json.loads(base64.b64decode(doc['content'])), doc['sha']
        except HTTPError as exc:
            if exc.code == 404:
                return None, None
            raise

    def write(self, state, sha):
        body = dict(message='Record shared API-Football allocation [skip ci]', branch=BRANCH,
                    content=base64.b64encode((json.dumps(state, sort_keys=True)+'\n').encode()).decode())
        if sha:
            body['sha'] = sha
        try:
            request = Request(self.url, data=json.dumps(body).encode(),
                              headers={**self.headers(), 'Content-Type': 'application/json'}, method='PUT')
            with self.opener(request, timeout=20) as response:
                response.read()
            return True
        except HTTPError as exc:
            if exc.code in (409, 422):
                return False
            raise


class Ledger:
    def __init__(self, store, pause=time.sleep):
        self.store, self.pause = store, pause

    def claim(self, day, used, limit, tier, lease_id, owner, closing=None):
        for attempt in range(10):
            state, sha = self.store.read()
            state, amount = allocate(state, day, used, limit, tier, lease_id, owner, closing)
            state['updatedAtUtc'] = utcnow().isoformat()
            if self.store.write(state, sha):
                return amount
            self.pause(min(.15*(attempt+1), 1.5))
        raise ValueError('Shared budget is busy')

    def finish(self, day, lease_id, consumed):
        for attempt in range(10):
            state, sha = self.store.read()
            if not state or state.get('dayUtc') != day or lease_id not in state.get('leases', {}):
                return
            lease = state['leases'].pop(lease_id)
            if not 0 <= consumed <= lease['reserved']:
                raise ValueError('Invalid lease completion')
            state['committed'] += consumed
            if self.store.write(state, sha):
                return
            self.pause(.1*(attempt+1))
        # Leaving the entire lease charged is conservative; never reclaim it by age.
        raise ValueError('Could not close shared allocation')


class Runtime:
    def __init__(self, opener):
        self.opener = opener
        self.ledger = Ledger(GithubStore(opener))
        self.day = self.lease = self.tier = None
        self.amount = self.consumed = 0
        self.owner = '/'.join(os.getenv(k, 'local') for k in ('GITHUB_RUN_ID', 'GITHUB_JOB'))+'/'+uuid.uuid4().hex
        path = os.getenv('STATMAKER_API_BUDGET_POLICY')
        self.policy = json.loads(Path(path).read_text()) if path else {}
        atexit.register(self.finish)

    def provider_status(self, key):
        request = Request('https://'+HOST+'/status', headers={'x-apisports-key': key}, method='GET')
        with self.opener(request, timeout=20) as response:
            doc = json.loads(response.read())
        if doc.get('errors'):
            raise ValueError('Provider quota is unavailable')
        values = doc['response']['requests']
        used, limit = values['current'], values['limit_day']
        if isinstance(used, bool) or not isinstance(used, int) or not isinstance(limit, int):
            raise ValueError('Invalid provider quota counters')
        return used, limit

    def classify(self, url):
        if _tier.get():
            return _tier.get()
        profile = os.getenv('STATMAKER_API_BUDGET_PROFILE', 'background')
        if profile == 'background':
            return 'background'
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if parsed.path.rstrip('/') == '/fixtures' and 'date' in params:
            return 'primary'
        league = (params.get('league') or [''])[0]
        row = self.policy.get('leagueIds', {}).get(str(league), {})
        if row and (params.get('season') or [str(row['season'])])[0] != str(row['season']):
            return 'background'
        return row.get('tier', 'background')

    def take(self, request):
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        tier, day = self.classify(url), utcnow().date().isoformat()
        headers = dict((k.lower(), v) for k, v in request.header_items()) if hasattr(request, 'header_items') else {}
        key = headers.get('x-apisports-key') or os.getenv('API_FOOTBALL_KEY')
        if not key:
            raise ValueError('Missing provider key')
        if day != self.day or tier != self.tier or self.consumed >= self.amount:
            closing = (self.lease, self.consumed) if self.lease and self.day == day else None
            if self.lease and self.day == day and tier != self.tier:
                self.ledger.finish(self.day, self.lease, self.consumed)
                closing = None
                self.lease = None
            used, limit = self.provider_status(key)
            lease = self.owner+'/'+uuid.uuid4().hex
            amount = self.ledger.claim(day, used, limit, tier, lease, self.owner, closing)
            self.day, self.lease, self.tier = day, lease, tier
            self.amount, self.consumed = amount, 0
        self.consumed += 1

    def finish(self):
        if self.lease:
            try:
                self.ledger.finish(self.day, self.lease, self.consumed)
                self.lease = None
            except Exception:
                print('API_FOOTBALL_BUDGET_LEASE_RETAINED conservatively charged', flush=True)


def install():
    global _original_open
    import urllib.request
    if getattr(urllib.request.urlopen, '_statmaker_budget', False):
        return
    _original_open = urllib.request.urlopen

    def guarded(request, *args, **kwargs):
        global _runtime
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        parsed = urlparse(url)
        if parsed.hostname == HOST and parsed.path.rstrip('/') != '/status':
            if os.getenv('STATMAKER_API_BUDGET_DEPLOY_ONLY') == 'true':
                raise BudgetStop('policy-only deployment; no provider work')
            try:
                with _lock:
                    if _runtime is None:
                        _runtime = Runtime(_original_open)
                    _runtime.take(request)
            except BudgetStop:
                raise
            except Exception:
                raise BudgetStop('shared quota or allocation unavailable; fail closed') from None
        return _original_open(request, *args, **kwargs)

    guarded._statmaker_budget = True
    urllib.request.urlopen = guarded
