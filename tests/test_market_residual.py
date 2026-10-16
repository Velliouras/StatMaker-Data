import copy,sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.market_residual import fit_residual,probabilities,groups


class MarketResidualTests(unittest.TestCase):
    def test_market_identity_and_normalized_binary_and_three_way_predictions(self):
        for p,q in [([.7,.3],[.5,.5]),([.6,.2,.2],[.4,.3,.3])]:
            np.testing.assert_allclose(probabilities(p,q,[0,0]),q)
            np.testing.assert_allclose(probabilities(p,q,[1,0]),p)
            self.assertAlmostEqual(probabilities(p,q,[-.6,.2]).sum(),1.)

    def test_negative_statistical_deviation_is_learned_from_prior_labels_not_manual_reversal(self):
        rows=[dict(fixture=str(i),kickoff=i,stat=[.8,.2],market=[.5,.5],y=[0.,1.]) for i in range(100)]
        fit=fit_residual(rows,100);self.assertLess(fit['coef'][0],-.5)
        p=probabilities([.8,.2],[.5,.5],fit['coef']);self.assertGreater(p[1],p[0])
        with self.assertRaises(ValueError):fit_residual(rows,99)
        self.assertEqual(fit,fit_residual([dict(r,profit=999.,odd=999.) for r in rows],100))

    def test_fixture_weighting_is_invariant_to_duplicating_every_partition(self):
        rows=[dict(fixture=str(i),kickoff=i,stat=[.6,.4],market=[.5,.5],y=[float(i%3>0),float(i%3==0)]) for i in range(100)]
        np.testing.assert_allclose(fit_residual(rows,100)['coef'],fit_residual(rows+copy.deepcopy(rows),100)['coef'],atol=1e-8)

    def test_prediction_partitions_do_not_disappear_when_event_later_pushes(self):
        base=dict(fixture='f',sourceReceipt='r',bookmaker='A',kickoff=1.,push=.1,actualPush=0.,reference=dict(q=.5))
        rows=[dict(base,spec=['CORNERS','TOTAL','OVER','',10.],p=.6,y=1.),dict(base,spec=['CORNERS','TOTAL','UNDER','',10.],p=.3,y=0.)]
        pushed=[dict(r,y=0.,actualPush=1.) for r in rows]
        first=groups(rows,for_training=False);second=groups(pushed,for_training=False)
        self.assertEqual(len(first),1);self.assertEqual(len(second),1)
        self.assertEqual(first[0]['stat'],second[0]['stat']);self.assertEqual(first[0]['market'],second[0]['market'])
        self.assertFalse(groups(pushed))

if __name__=='__main__':unittest.main()
