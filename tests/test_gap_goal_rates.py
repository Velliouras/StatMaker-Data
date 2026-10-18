import copy,math,sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.gap_goal_rates import FEATURES,features,fit,bank,predict

class GapGoalTests(unittest.TestCase):
    def rows(self):
        rng=np.random.default_rng(23);rows=[]
        for i in range(160):
            signal=float(i%2);means=np.array([1.5,1.1]);x=[[1.,signal,signal,signal,1.],[1.,-signal,-signal,-signal,1.]]
            actual=rng.poisson(means*np.exp([.1+.15*signal,.1-.15*signal])).tolist()
            rows.append(dict(fixture=str(i),kickoff=100000.+i,featureCutoff=0.,x=x,baseMeanBank=[means.tolist()]*13,actual=actual))
        return rows

    def test_feature_quality_and_cold_start_are_explicit(self):
        f={m:dict(home=v,away=v,sampleHome=0,sampleAway=0) for m,v in [('SHOTS',24.),('SOT',8.),('CORNERS',9.)]}
        self.assertEqual(features(f,'home'),[1.,0.,0.,0.,0.])

    def test_fit_uses_no_prices_or_goal_market_selection(self):
        r=self.rows();a=fit(r,300000.);b=fit([dict(v,odd=99.,roi=500) for v in r],300000.)
        self.assertEqual(a,b);self.assertTrue(a['fitted']);self.assertFalse(a['roiUsed']);self.assertTrue(np.isfinite(predict(r[0],a)).all())

    def test_duplicate_fixtures_have_fixed_training_weight(self):
        r=self.rows();a=fit(r,300000.);b=fit(r+[copy.deepcopy(r[0])]*10,300000.)
        np.testing.assert_allclose(a['coef'],b['coef'],atol=1e-8)

    def test_future_labels_do_not_change_prior_bank(self):
        r=self.rows();f=dict(r[0],fixture='future',kickoff=299999.,actual=[99,99]);self.assertEqual(bank(r,300000.),bank(r+[f],300000.))
        with self.assertRaises(ValueError):fit(r+[f],300000.)

    def test_baseline_offsets_are_preserved_for_zero_coefficients(self):
        r=self.rows()[0];p=dict(fitted=True,coef=[0.]*len(FEATURES));np.testing.assert_allclose(predict(r,p),r['baseMeanBank'][0])

if __name__=='__main__':unittest.main()
