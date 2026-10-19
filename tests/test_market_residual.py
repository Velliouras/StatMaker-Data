import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.market_residual import fit,bank,predict,candidate

class MarketResidualTests(unittest.TestCase):
    def rows(self):return [dict(fixture=str(i),metric='SHOTS',kickoff=100000.+i,featureCutoff=0.,statOver=.75,marketOver=.5,yOver=float(i%2)) for i in range(40)]
    def test_zero_offset_weight_is_market_and_unit_weight_is_stat(self):
        self.assertAlmostEqual(predict(.8,.6,[0.,0.]),.6);self.assertAlmostEqual(predict(.8,.6,[0.,1.]),.8)
    def test_market_is_retained_when_stats_have_no_signal(self):
        r=fit(self.rows(),300000.);self.assertTrue(r['fitted']);self.assertFalse(r['roiUsed']);self.assertAlmostEqual(r['theta'][1],0.,places=6)
    def test_future_outcomes_cannot_change_bank(self):
        rows=self.rows();expected=bank(rows,'SHOTS',300000.);self.assertEqual(bank(rows+[dict(rows[0],kickoff=299999.,yOver=1.)],'SHOTS',300000.),expected)
    def test_training_boundary_and_negative_weight_are_rejected(self):
        with self.assertRaises(ValueError):fit(self.rows(),100001.)
        with self.assertRaises(ValueError):predict(.8,.6,[0.,-.1])
    def test_monotone_and_complementary_sides(self):
        theta=[.1,.3];values=[predict(p,.5,theta) for p in [.1,.3,.5,.7,.9]];self.assertEqual(values,sorted(values));self.assertAlmostEqual(predict(.7,.5,[0.,.3])+predict(.3,.5,[0.,.3]),1.)
    def test_fewer_prior_fixtures_is_inactive(self):self.assertFalse(fit(self.rows()[:29],300000.)['fitted'])
    def test_decision_never_raises_raw_probability_or_lower(self):
        q=dict(p=.7,push=.05,odd=2.,confidence=.7,spec=['SHOTS','TOTAL','OVER','',20.5],_lower=dict(p=.65,push=.01,support=.8))
        fit=dict(point=dict(theta=[.2,1.]),replicas=[dict(theta=[.2,1.]) for _ in range(12)])
        r=candidate(q,{'q':.65},fit);self.assertLessEqual(r['p'],q['p']);self.assertLessEqual(r['_lower']['p'],q['_lower']['p']);self.assertEqual(r['_lower']['support'],.8)

if __name__=='__main__':unittest.main()
