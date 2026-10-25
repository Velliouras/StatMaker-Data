import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts/api_budget_runtime'))
import api_football_budget as budget


class Store:
    def __init__(self, state=None):
        self.state, self.sha, self.lock = state, 0, threading.Lock()
    def read(self):
        with self.lock:return copy.deepcopy(self.state), self.sha
    def write(self, state, sha):
        with self.lock:
            if sha != self.sha:return False
            self.state, self.sha = copy.deepcopy(state), self.sha+1
            return True


class SharedBudgetTest(unittest.TestCase):
    def claim(self, store, name='one', used=0, tier='primary', closing=None):
        return budget.Ledger(store, pause=lambda _:None).claim('2026-10-25',used,7500,tier,name,name,closing)

    def test_parallel_actions_share_the_last_tokens(self):
        store=Store();self.claim(store,'initial',used=5980)
        def request(i):
            try:return self.claim(store,'parallel-'+str(i),used=5980)
            except budget.BudgetStop:return 0
        with ThreadPoolExecutor(max_workers=12) as pool:amounts=list(pool.map(request,range(30)))
        self.assertEqual(sum(amounts),12)
        self.assertEqual(store.state['committed']+sum(x['reserved'] for x in store.state['leases'].values()),6000)

    def test_background_stops_before_priority(self):
        store=Store()
        self.assertEqual(self.claim(store,used=4498,tier='background'),2)
        with self.assertRaises(budget.BudgetStop):self.claim(store,'blocked',used=4498,tier='background')
        self.assertEqual(self.claim(store,'priority',used=4500),8)

    def test_unused_tokens_return_only_on_explicit_completion(self):
        store=Store();self.claim(store,used=5990)
        budget.Ledger(store).finish('2026-10-25','one',2)
        self.assertEqual(store.state['committed'],5992)
        self.assertEqual(store.state['estimatedAllocated'],5992)
        self.assertEqual(self.claim(store,'two',used=5992),8)

    def test_killed_worker_reservation_is_not_reclaimed_by_age(self):
        store=Store();self.claim(store,used=5992)
        with self.assertRaises(budget.BudgetStop):self.claim(store,'other',used=5992)

    def test_provider_counter_and_other_account_usage_are_respected(self):
        store=Store();self.claim(store,used=100)
        with self.assertRaises(budget.BudgetStop):self.claim(store,'later',used=6000)

    def test_next_utc_day_resets_old_reservations(self):
        store=Store();self.claim(store,used=5992)
        amount=budget.Ledger(store).claim('2026-10-26',0,7500,'primary','new','new')
        self.assertEqual(amount,8);self.assertEqual(store.state['committed'],0)
        self.assertEqual(set(store.state['leases']),{'new'})

    def test_process_rollover_commits_used_block_once(self):
        store=Store();self.claim(store,used=100)
        self.claim(store,'next',used=108,closing=('one',8))
        self.assertEqual(store.state['committed'],108)
        self.assertEqual(set(store.state['leases']),{'next'})

    def test_lower_subscription_limit_still_preserves_reserve(self):
        with self.assertRaises(budget.BudgetStop):budget.allocate(None,'2026-10-25',0,100,'primary','a','a')

    def test_unknown_status_or_corrupt_budget_never_allocates(self):
        for value in (None,True,-1,7501):
            with self.subTest(value=value),self.assertRaises(ValueError):budget.allocate(None,'2026-10-25',value,7500,'primary','a','a')
        for committed, reserved in ((-1, 8), (0, -5), (0, 9000)):
            state=dict(schemaVersion=1,dayUtc='2026-10-25',committed=committed,leases={'broken':{'reserved':reserved}})
            with self.assertRaises(ValueError):budget.allocate(state,'2026-10-25',0,7500,'primary','a','a')

    def test_guard_refuses_paid_http_when_shared_ledger_is_unavailable(self):
        import urllib.request
        with mock.patch.object(urllib.request,'urlopen') as opener, \
                mock.patch.object(budget,'Runtime',side_effect=ValueError('unavailable')), \
                mock.patch.object(budget,'_runtime',None), \
                mock.patch.dict(os.environ,{'STATMAKER_API_BUDGET_DEPLOY_ONLY':'false'}):
            opener._statmaker_budget = False
            budget.install()
            with self.assertRaises(budget.BudgetStop):
                urllib.request.urlopen('https://'+budget.HOST+'/fixtures?league=39&season=2026')
            opener.assert_not_called()
            urllib.request.urlopen('https://'+budget.HOST+'/status')
            opener.assert_called_once()

    def test_new_day_cannot_reuse_yesterdays_remaining_tokens(self):
        runtime=budget.Runtime.__new__(budget.Runtime)
        runtime.day='2026-10-24';runtime.lease='old';runtime.tier='primary'
        runtime.amount=8;runtime.consumed=1;runtime.owner='worker';runtime.policy={}
        runtime.ledger=mock.Mock();runtime.ledger.claim.return_value=8
        runtime.provider_status=mock.Mock(return_value=(0,7500))
        request=budget.Request('https://'+budget.HOST+'/fixtures?date=2026-10-25',headers={'x-apisports-key':'test'})
        with mock.patch.object(budget,'utcnow',return_value=budget.datetime(2026,10,25,tzinfo=budget.timezone.utc)), \
                mock.patch.dict(os.environ,{'STATMAKER_API_BUDGET_PROFILE':'rolling'}):runtime.take(request)
        self.assertEqual(runtime.day,'2026-10-25');self.assertEqual(runtime.consumed,1)
        self.assertEqual(runtime.ledger.claim.call_args.args[0],'2026-10-25')

    def test_python_startup_installs_guard_before_provider_import(self):
        env={**os.environ,'PYTHONPATH':str(ROOT/'scripts/api_budget_runtime'),'STATMAKER_API_BUDGET_ENFORCE':'true'}
        result=subprocess.run([sys.executable,'-c','import urllib.request; assert urllib.request.urlopen._statmaker_budget'],env=env,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode())


if __name__=='__main__':unittest.main()
