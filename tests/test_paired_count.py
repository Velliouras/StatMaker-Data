import sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.paired_count import from_means
from betting_v2.joint_count import JointCount
from betting_v2.validated_walk_forward import distribution


class PairedCountTests(unittest.TestCase):
    def test_total_and_team_means_and_probabilities_are_consistent(self):
        j=from_means(6.,4.,alpha=.1,concentration=20.)
        self.assertAlmostEqual(j.pmf.sum(),1.)
        np.testing.assert_allclose(j.means,[6.,4.],rtol=1e-8,atol=1e-8)
        self.assertAlmostEqual(sum(j.evaluate(('CORNERS','TOTAL',side,'',9.5))[0] for side in ('OVER','UNDER')),1.)
        self.assertAlmostEqual(sum(j.evaluate(('CORNERS','RESULT',side,'',None))[0] for side in ('HOME','DRAW','AWAY')),1.)

    def test_both_negative_allocation_and_positive_tempo_dependence_are_supported(self):
        for alpha,expected in [(0.,-1),(.2,1)]:
            j=from_means(6.,4.,alpha=alpha,concentration=10.);h,a=np.indices(j.pmf.shape)
            covariance=float((j.pmf*h*a).sum()-j.means[0]*j.means[1]);self.assertEqual(np.sign(covariance),expected)

    def test_old_profiles_are_unchanged_and_new_profile_is_explicit(self):
        row=dict(metric='CORNERS',regime='XG',means=[6.,4.])
        np.testing.assert_array_equal(distribution(row,{}).pmf,JointCount.from_means(6.,4.).pmf)
        profile={'CORNERS::XG':dict(family='total_allocation',scale=1.,alpha=.1,concentration=20.)}
        np.testing.assert_array_equal(distribution(row,profile).pmf,from_means(6.,4.,alpha=.1,concentration=20.).pmf)

if __name__=='__main__':unittest.main()
