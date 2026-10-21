import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from .results import cached_rows,exact_result
from .feed import settle,timestamp

class ExactCachedResultsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.path=self.root/'data/statmaker/uefa_current_stats.json';self.path.parent.mkdir(parents=True)
        self.row=dict(apiFixtureId=1635702,kickoff='2026-10-20T19:00:00Z',publishedAt='2026-10-19T19:49:58Z',
          outcome='PENDING',metric='sot',scope='TOTAL',side='UNDER',teamSide='',line=11.5,odd=1.91)
        self.now=timestamp('2026-10-21T06:20:00Z')
        self.fixture=dict(fixtureId=1635702,kickoff='2026-10-20T19:00:00+00:00',status='FT',normalizedStats=dict(HS=22,AS=18,HST=14,AST=5))
    def tearDown(self):self.tmp.cleanup();cached_rows.cache_clear()
    def write(self,fixture=None):
        self.path.write_text(json.dumps(dict(generatedAt='2026-10-21T06:03:29Z',competitions=[dict(fixtures=[fixture or self.fixture])])));cached_rows.cache_clear()
    def test_uefa_settles_without_fixture_id_in_prediction_history(self):
        self.write();before=copy.deepcopy(self.row)
        history=[dict(_date=timestamp(self.row['kickoff']),normalized_stats=self.fixture['normalizedStats'])]
        with patch('scripts.shots_uat.feed.ROOT',self.root):resolved=settle(self.row,history,self.now)
        self.assertEqual(resolved['outcome'],'LOST');self.assertEqual(resolved['actualCount'],19)
        self.assertEqual(resolved['odd'],1.91);self.assertEqual(self.row,before)
        self.assertIn('uefa_current_stats.json',resolved['settlementSource'])
    def test_different_fixture_same_teams_or_wrong_kickoff_cannot_settle(self):
        for f in [dict(self.fixture,fixtureId=111),dict(self.fixture,kickoff='2026-10-19T19:00:00Z')]:
            self.write(f);self.assertIsNone(exact_result(self.row,[],self.root,self.now))
    def test_non_final_extra_time_and_missing_or_inconsistent_counts_remain_pending(self):
        for f in [dict(self.fixture,status='2H'),dict(self.fixture,status='AET'),dict(self.fixture,normalizedStats=dict(HS=22,AS=18)),dict(self.fixture,normalizedStats=dict(HS=2,AS=18,HST=14,AST=5))]:
            self.write(f);self.assertIsNone(exact_result(self.row,[],self.root,self.now))
    def test_fresher_exact_cache_uses_latest_completed_result_not_old_label(self):
        self.write();old=dict(fixture_id=1635702,_date=timestamp(self.row['kickoff']),normalized_stats=dict(HS=22,AS=18,HST=15,AST=5))
        self.assertEqual(exact_result(self.row,[old],self.root,self.now)['normalized_stats']['HST'],14)
    def test_pending_result_does_not_modify_published_probability_or_quote(self):
        self.write();self.row.update(metric='shots',side='OVER',line=40.)
        with patch('scripts.shots_uat.feed.ROOT',self.root):r=settle(self.row,[],self.now)
        self.assertEqual(r['outcome'],'VOID');self.assertEqual(r['settlementReturn'],1.0)
        self.assertEqual(r['line'],40.);self.assertEqual(r['publishedAt'],self.row['publishedAt'])

if __name__=='__main__':unittest.main()
