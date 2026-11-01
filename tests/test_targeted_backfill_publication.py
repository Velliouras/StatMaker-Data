"""Execute the actual workflow publication shell with local command stubs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]

class TargetedBackfillPublicationTest(unittest.TestCase):
    def test_generated_reports_are_staged_not_executed_and_derived_products_follow_rebase(self):
        doc=yaml.safe_load((ROOT/'.github/workflows/domestic-targeted-stats-backfill.yml').read_text())
        job=doc['jobs']['targeted-backfill']
        script=next(step['run'] for step in job['steps'] if step.get('name')=='Commit targeted stats artifacts')
        with tempfile.TemporaryDirectory(prefix='backfill publication ') as directory:
            root=Path(directory);commands=root/'command-log.jsonl';bin_dir=root/'bin';bin_dir.mkdir()
            reports=root/'reports';reports.mkdir()
            report=reports/'api_football_fixture_stats_fetch.json';report.write_text('{}');report.chmod(0o600)
            stub='#!'+sys.executable+'\n'+'''import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
with open(os.environ['PUBLICATION_COMMAND_LOG'],'a') as handle:
    handle.write(json.dumps([name]+sys.argv[1:])+'\\n')
if name=='git' and sys.argv[1:]==['diff','--cached','--quiet']:
    sys.exit(1)
sys.exit(0)
'''
            for name in ('git','python'):
                path=bin_dir/name;path.write_text(stub);path.chmod(0o700)
            result=subprocess.run(['/bin/bash','-e','-c',script],cwd=root,
                env={**os.environ,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH'],
                     'PUBLICATION_COMMAND_LOG':str(commands)},capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stdout+'\n'+result.stderr)
            calls=[json.loads(line) for line in commands.read_text().splitlines()]
        stages=[call for call in calls if call[:2]==['git','add']]
        source=stages[0]
        self.assertIn('reports/api_football_fixture_stats_fetch.json',source)
        self.assertIn('reports/domestic_targeted_stats_backfill.log',source)
        self.assertIn('data/statmaker/domestic_enriched',source)
        self.assertNotIn('data/statmaker/update_manifest.json',source)
        self.assertNotIn('data/statmaker/uefa_support_history.json',source)
        pull=next(i for i,c in enumerate(calls) if c[:2]==['git','pull'])
        uefa=next(i for i,c in enumerate(calls) if c[:2]==['python','scripts/build_uefa_support_history.py'])
        manifest=next(i for i,c in enumerate(calls) if c[:2]==['python','scripts/build_statmaker_update_manifest.py'])
        push=next(i for i,c in enumerate(calls) if c[:2]==['git','push'])
        self.assertLess(pull,uefa);self.assertLess(uefa,manifest);self.assertLess(manifest,push)

if __name__=='__main__':unittest.main()
