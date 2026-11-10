"""Exercise concurrent cache publication using real shallow Git checkouts."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import yaml
from scripts import publish_targeted_backfill as publisher

ROOT = Path(__file__).resolve().parents[1]
CACHE = 'data/api_football/fixture_stats/example/2026/fixture_stats.json'
DERIVED = 'data/statmaker/domestic_enriched/example.json'


@contextmanager
def cwd(path):
    old = Path.cwd()
    os.chdir(path)
    try: yield
    finally: os.chdir(old)


class TargetedBackfillPublicationTest(unittest.TestCase):
    def git(self, path, *args):
        result = subprocess.run(['git', *args], cwd=path, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def write(self, root, name, value):
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_workflow_calls_semantic_publisher_and_removes_retired_betting_tail(self):
        doc = yaml.safe_load((ROOT/'.github/workflows/domestic-targeted-stats-backfill.yml').read_text())
        steps = doc['jobs']['targeted-backfill']['steps']
        script = next(s['run'] for s in steps if s.get('name') == 'Commit targeted stats artifacts')
        self.assertIn('python -m scripts.publish_targeted_backfill', script)
        self.assertNotIn('git pull --rebase', script)
        checkout = next(s for s in steps if str(s.get('uses','')).startswith('actions/checkout@'))
        self.assertEqual(1, checkout['with']['fetch-depth'])
        self.assertEqual('blob:none', checkout['with']['filter'])
        full = (ROOT/'.github/workflows/app-ready-artifact-publisher.yml').read_text()
        self.assertNotIn('scripts.betting_v2.publish_uat_feed', full)
        self.assertIn('b4630638077b1c3dd83d94c66c03e2a70f45efd1', full)

    def test_daily_universe_uses_offline_source_merge_and_keeps_refresh_state(self):
        doc = yaml.safe_load((ROOT/'.github/workflows/domestic-live-july-pipeline.yml').read_text())
        script = next(s['run'] for s in doc['jobs']['refresh']['steps']
                      if s.get('name') == 'Commit artifacts and diagnostics')
        self.assertIn('python scripts/publish_targeted_backfill.py', script)
        self.assertNotIn('git pull --rebase', script)
        self.assertIn('data/statmaker/domestic_live_july_state.json', publisher.SOURCES)

    def test_three_way_merge_retains_both_fixtures_and_newer_conflicting_value(self):
        base = {'fixtures':[{'fixture_id':1,'normalized_stats':{'HS':2}},
                            {'fixture_id':2,'normalized_stats':{'HS':3}}]}
        current, incoming = deepcopy(base), deepcopy(base)
        current['fixtures'][1]['normalized_stats']['HS'] = 12
        incoming['fixtures'][0]['normalized_stats']['HS'] = 9
        merged = publisher.merge_delta(base, current, incoming)
        self.assertEqual([9,12], [r['normalized_stats']['HS'] for r in merged['fixtures']])
        incoming['fixtures'][1]['normalized_stats']['HS'] = 6
        self.assertEqual(12, publisher.merge_delta(base,current,incoming)['fixtures'][1]['normalized_stats']['HS'])
        self.assertEqual(merged, publisher.merge_delta(base,merged,incoming))

    def test_raw_statistics_merge_by_team_and_type_without_duplicate_blocks(self):
        base = [{'team':{'id':10},'statistics':[{'type':'Shots','value':2}]}]
        current = [{'team':{'id':10},'statistics':[{'type':'Shots','value':5}]}]
        incoming = [{'team':{'id':10},'statistics':[{'type':'Shots','value':3},{'type':'Corners','value':4}]}]
        merged = publisher.merge_delta(base,current,incoming)
        self.assertEqual(1,len(merged))
        self.assertEqual({'Shots':5,'Corners':4},{r['type']:r['value'] for r in merged[0]['statistics']})

    def test_concurrent_same_file_change_rebuilds_derived_products_from_merged_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin, writer, producer = (root/n for n in ('origin.git','writer','producer'))
            self.git(root,'init','--bare','--initial-branch=main',str(origin))
            self.git(root,'clone',str(origin),str(writer))
            self.git(writer,'config','user.name','Test'); self.git(writer,'config','user.email','test@example.test')
            base = {'fixtures':[{'fixture_id':1,'value':2},{'fixture_id':2,'value':3}]}
            self.write(writer,'data/statmaker/domestic_live_july_state.json',{'completed':1})
            self.write(writer,CACHE,base); self.write(writer,DERIVED,{'total':5})
            self.git(writer,'add','.'); self.git(writer,'commit','-m','initial'); self.git(writer,'push','origin','main')
            self.git(root,'clone','--depth=1',origin.as_uri(),str(producer))
            self.git(producer,'config','user.name','Test'); self.git(producer,'config','user.email','test@example.test')
            self.assertEqual('true',self.git(producer,'rev-parse','--is-shallow-repository'))
            self.write(producer,'data/statmaker/domestic_live_july_state.json',{'completed':2})
            incoming = deepcopy(base); incoming['fixtures'][0]['value'] = 9
            self.write(producer,CACHE,incoming); self.write(producer,DERIVED,{'total':12})
            newer = deepcopy(base); newer['fixtures'][1]['value'] = 12
            self.write(writer,CACHE,newer); self.write(writer,DERIVED,{'total':14})
            self.git(writer,'add','.'); self.git(writer,'commit','-m','concurrent stats'); self.git(writer,'push','origin','main')
            upstream = self.git(writer,'rev-parse','HEAD')
            def rebuild():
                doc = json.loads(Path(CACHE).read_text())
                self.write(producer,DERIVED,{'total':sum(r['value'] for r in doc['fixtures'])})
            with cwd(producer): publisher.publish(rebuild_outputs=rebuild, pause=lambda _:None)
            self.assertEqual([9,12],[r['value'] for r in json.loads((producer/CACHE).read_text())['fixtures']])
            self.assertEqual({'completed':2},json.loads((producer/'data/statmaker/domestic_live_july_state.json').read_text()))
            self.assertEqual({'total':21},json.loads((producer/DERIVED).read_text()))
            self.assertEqual('1',self.git(producer,'rev-list','--count',upstream+'..HEAD'))
            self.assertEqual(self.git(producer,'rev-parse','HEAD'),self.git(origin,'rev-parse','main'))


if __name__ == '__main__': unittest.main()
