import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts/api_budget_runtime'))
from scripts.refresh_priority_matches import refresh, merge_matches, standings, due, current_policy, FOLDER
from api_football_budget import BudgetStop

NOW = datetime(2026,10,25,19,0,tzinfo=timezone.utc)


def fixture(fid=1, league=39, season=2026, status='FT', goals=(2,1)):
    return {'fixture':{'id':fid,'date':NOW.isoformat(),'status':{'short':status}},
            'league':{'id':league,'season':season,'round':'Regular Season - 10'},
            'teams':{'home':{'id':10,'name':'Provider Home'},'away':{'id':11,'name':'Away'}},
            'goals':{'home':goals[0],'away':goals[1]}}


class FakeProvider:
    def __init__(self, stop=None):self.calls=0;self.requests=[];self.stop=stop
    def get(self, endpoint, params, tier):
        if tier==self.stop:raise BudgetStop('test exhausted')
        self.calls+=1;self.requests.append((endpoint,params,tier))
        return [fixture(league=params.get('league',39))] if endpoint=='fixtures' else []


class PriorityRefreshTest(unittest.TestCase):
    def setup_root(self, root):
        tiers={'primary':{'resultsMinutes':15,'standingsMinutes':30},'europe':{'resultsMinutes':30,'standingsMinutes':60},'background':{'resultsMinutes':60,'standingsMinutes':120}}
        rows={
              str(i):dict(code=c,season=2026,country=c,competition=c,displaySeason='2026-2027',tier=t)
              for i,c,t in ((39,'E0','primary'),(94,'P1','europe'),(71,'B1','background'))}
        (root/'config').mkdir()
        (root/'config/api_football_refresh_policy.json').write_text(json.dumps({'tiers':tiers,'leagueIds':rows}))

    def run_refresh(self, root, provider, now=NOW):
        with contextlib.redirect_stdout(io.StringIO()):return refresh(root,provider,now)

    def test_all_three_tiers_and_no_early_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_root(root);first=FakeProvider();self.run_refresh(root,first)
            self.assertEqual([t for e,p,t in first.requests if 'season' in p and e=='fixtures'],['primary','europe','background'])
            second=FakeProvider();self.run_refresh(root,second,NOW+timedelta(minutes=8))
            self.assertEqual(second.calls,0)
            third=FakeProvider();self.run_refresh(root,third,NOW+timedelta(minutes=15))
            self.assertEqual(third.calls,3)
            e=json.loads((root/FOLDER/'E0.json').read_text());p=json.loads((root/FOLDER/'P1.json').read_text())
            self.assertNotEqual(e['resultsCheckedAtUTC'],p['resultsCheckedAtUTC'])

    def test_five_minute_and_event_wakeups_do_not_multiply_provider_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_root(root);self.run_refresh(root,FakeProvider())
            calls=[]
            for minute in (5,8,10,15,16,20,25,30):
                provider=FakeProvider();self.run_refresh(root,provider,NOW+timedelta(minutes=minute))
                calls.append(provider.calls)
            # Only the 15-minute results bucket and 30-minute table bucket spend quota.
            self.assertEqual(calls,[0,0,0,3,0,0,0,4])

    def test_completion_fallback_has_no_self_or_watchdog_loop(self):
        doc=yaml.safe_load((ROOT/'.github/workflows/priority-results-standings.yml').read_text())
        triggers=doc.get('on',doc.get(True))
        self.assertEqual(triggers['schedule'],[{'cron':'2-59/5 * * * *'}])
        fallback=triggers['workflow_run']
        self.assertEqual(fallback['types'],['completed']);self.assertEqual(fallback['branches'],['main'])
        self.assertEqual(fallback['workflows'], ['Domestic Lineup Watch'])
        self.assertNotIn('UEFA Current Stats Refresh', fallback['workflows'])
        self.assertNotIn(doc['name'],fallback['workflows'])
        self.assertNotIn('Live Settlement Watchdog',fallback['workflows'])
        self.assertIn('workflow_run.head_repository.full_name == github.repository',doc['jobs']['refresh']['if'])
        self.assertEqual(doc['concurrency']['group'],'statmaker-priority-results-standings')
        self.assertFalse(doc['concurrency']['cancel-in-progress'])

    def test_background_quota_stop_keeps_verified_priority_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_root(root);index=self.run_refresh(root,FakeProvider(stop='background'))
            self.assertTrue(index['providerStops'])
            self.assertEqual(json.loads((root/FOLDER/'E0.json').read_text())['fixtures'][0]['home_goals'],2)

    def test_full_season_preserved_across_partial_date_response(self):
        old=merge_matches([], [fixture(i) for i in range(1,81)],39,2026,'E0',{10:'Canonical Home'})
        old[0]['normalized_stats']={'HS':15}
        merged=merge_matches(old,[fixture(1,goals=(3,1)),fixture(500,season=2025)],39,2026,'E0',{10:'Canonical Home'})
        self.assertEqual(len(merged),80);self.assertEqual(merged[0]['home_team'],'Canonical Home')
        self.assertEqual(merged[0]['normalized_stats'],{'HS':15});self.assertEqual(merged[0]['home_goals'],3)

    def test_unverified_scores_do_not_erase_finals(self):
        old=merge_matches([], [fixture()],39,2026,'E0',{})
        for bad in (fixture(status='NS',goals=(None,None)),fixture(goals=(None,None))):
            self.assertEqual(merge_matches(old,[bad],39,2026,'E0',{}),old)

    def test_correct_season_and_multi_group_standings(self):
        item={'rank':1,'team':{'id':10,'name':'Provider Home'},'all':{'played':4,'win':3,'draw':1,'lose':0,'goals':{'for':8,'against':2}},'points':10}
        raw=[{'league':{'id':39,'season':2026,'standings':[[item],[{**item,'team':{'id':11,'name':'Away'}}]]}}]
        table,single=standings(raw,39,2026,{10:'Canonical Home'})
        self.assertEqual(table[0]['team'],'Canonical Home');self.assertFalse(single)
        self.assertEqual(standings(raw,39,2025,{}),([],False))

    def test_utc_and_cadence_boundaries(self):
        self.assertFalse(due(NOW.isoformat(),15,NOW+timedelta(minutes=14,seconds=59)))
        self.assertTrue(due(NOW.isoformat(),15,NOW+timedelta(minutes=15)))
        self.assertTrue(due((NOW+timedelta(seconds=90)).isoformat(),15,NOW+timedelta(minutes=15,seconds=30)))
        self.assertFalse(due((NOW+timedelta(minutes=1)).isoformat(),15,NOW+timedelta(minutes=8)))

    def test_season_rollover_uses_the_catalogue_including_single_year_leagues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.setup_root(root)
            folder=root/'data/statmaker/domestic_enriched';folder.mkdir(parents=True)
            rows=[dict(league_code=c,lifecycle='active',stats_role='current_target',api_football_league_id=i,
                       api_football_season=y,app_season=label,country=country,league=c)
                  for c,i,y,label,country in [('E0',39,2027,'2027-2028','England'),('SWE',113,2027,'2027','Sweden')]]
            (folder/'index.json').write_text(json.dumps({'leagues':rows}))
            policy=current_policy(root,json.loads((root/'config/api_football_refresh_policy.json').read_text()))
            self.assertEqual(policy['leagueIds']['39']['season'],2027)
            self.assertEqual(policy['leagueIds']['113']['displaySeason'],'2027')

    def test_every_provider_workflow_has_startup_guard_and_write_permission(self):
        found=[]
        for p in (ROOT/'.github/workflows').glob('*.yml'):
            text=p.read_text()
            if 'API_FOOTBALL_KEY' not in text:continue
            found.append(p.name);doc=yaml.safe_load(text)
            self.assertEqual(str(doc['env']['STATMAKER_API_BUDGET_ENFORCE']).lower(),'true',p.name)
            self.assertIn('scripts/api_budget_runtime',doc['env']['PYTHONPATH'],p.name)
            self.assertEqual(doc['permissions']['contents'],'write',p.name)
            for job in doc['jobs'].values():
                for step in job.get('steps',[]):
                    sparse=step.get('with',{}).get('sparse-checkout')
                    if sparse:self.assertTrue('/scripts/' in sparse or 'scripts/api_budget_runtime/' in sparse,p.name)
                    for line in step.get('run','').splitlines():
                        if 'PYTHONPATH=' in line:self.assertIn('api_budget_runtime',line,p.name)
        self.assertGreaterEqual(len(found),30)


if __name__=='__main__':unittest.main()
