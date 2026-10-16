import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.research_model_revision import read
from betting_v2.score_forward_research import settled
from betting_v2.forward_candidate import public_rows


class ForwardCandidateTests(unittest.TestCase):
    def test_only_publicly_committed_before_kickoff_rows_are_eligible(self):
        rows=[dict(kickoff=10.),dict(kickoff=20.)]
        self.assertEqual(public_rows(rows,15.),[rows[1]])
        self.assertEqual(public_rows(rows,None),[])
    def test_missing_result_is_unscored_not_a_loss(self):
        r=dict(fixture='f',observed=10.,kickoff=30.,spec=['CORNERS','TOTAL','OVER','',9.5])
        rows,missing=settled([r],{},20.);self.assertEqual(rows,[]);self.assertEqual(missing,1)

    def test_snapshot_must_precede_fixture_and_follow_observation(self):
        r=dict(fixture='f',observed=10.,kickoff=30.,spec=['GOALS_FT','TOTAL','OVER','',2.5])
        with self.assertRaises(ValueError):settled([r],{},30.)
        with self.assertRaises(ValueError):settled([r],{},9.)

    def test_integer_total_push_is_preserved_in_scoring(self):
        r=dict(fixture='f',observed=10.,kickoff=30.,spec=['CORNERS','TOTAL','OVER','',10.],odd=2.)
        truth=dict(normalized_stats=dict(HC=6,AC=4))
        rows,missing=settled([r],{'f':truth},20.);self.assertEqual(missing,0)
        self.assertEqual(rows[0]['y'],0.);self.assertEqual(rows[0]['actualPush'],1.)

if __name__=='__main__':unittest.main()
