import datetime as dt
import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import refresh_live_settlements as live

def issue():
 return dict(selectionId='s',apiFixtureId=1551005,kickoff='2026-10-21T03:00:00Z',publishedAt='2026-10-20T14:25:28Z',
  publicationCommit='a'*40,homeTeam='Tigres UANL',awayTeam='Leon',metric='sot',scope='TOTAL',market='MATCH_SHOTS_ON_TARGET',match=dict(leagueCode='MEX'))

class UatIssuedSettlementRequirements(unittest.TestCase):
 def parsed(self,row):
  with patch.object(live,'load_json',return_value=dict(rows=[row])):return live.requirements_from_uat_issues(Path('/test/ledger.json'))
 def test_published_uat_requires_its_exact_fixture_and_stat(self):
  rows=self.parsed(issue());self.assertEqual(len(rows),1)
  self.assertEqual(rows[0].api_fixture_id,1551005);self.assertEqual(rows[0].required_kind,'shots_on_target')
  self.assertEqual(rows[0].local_date,'2026-10-21');self.assertEqual(rows[0].generation_id,'uat-issued:s')
 def test_unpublished_late_or_unbound_entries_never_spend_quota(self):
  for changes in [dict(publishedAt=None),dict(publicationCommit=None),dict(publishedAt='2026-10-21T03:00:00Z'),dict(apiFixtureId=0)]:
   self.assertEqual(self.parsed(dict(issue(),**changes)),[])
 def test_card_units_and_unrecognised_metrics_do_not_become_generic_count_requests(self):
  self.assertEqual(self.parsed(dict(issue(),metric='cards')),[])
  self.assertEqual(self.parsed(dict(issue(),metric='invented')),[])
 def test_union_retains_existing_prod_and_hybrid_requirements(self):
  p=live.requirements_from_uat_issues
  with patch.object(live,'load_json',return_value=dict(rows=[issue()])):uat=p(Path('/test/ledger.json'))
  prod=live.SettlementRequirement('prod','domestic','p','2026-10-20','E0',1,('H',),('A',),'score','RESULT_1X2')
  hybrid=live.SettlementRequirement('hybrid','domestic','h','2026-10-20','E0',2,('H',),('A',),'shots','TEAM_SHOTS')
  with patch.object(live,'betting_bundle_paths',return_value=[]),patch.object(live,'requirements_from_canonical_ledger',side_effect=[[prod],[hybrid]]),patch.object(live,'requirements_from_uat_issues',side_effect=[uat,[]]):
   result=live.canonical_requirements()
  self.assertEqual({x.api_fixture_id for x in result},{1,2,1551005})

if __name__=='__main__':unittest.main()
