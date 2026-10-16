import json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.research_goal_factorized import (shifted_shares,compose_row,records,
    fit_offsets,conditional_loss,final_profile)
from betting_v2.validated_walk_forward import distribution
from betting_v2.research_goal_ridge import prior_design

class FactorizedGoalsTest(unittest.TestCase):
    def test_half_offsets_preserve_complements_and_zero_one_endpoints(self):
        a=np.asarray(shifted_shares([.4,.5],[.3,-.2]))
        b=np.asarray(shifted_shares([.6,.5],[.3,-.2],'GOALS_2H'))
        np.testing.assert_allclose(a+b,[1.,1.],atol=1e-15)
        self.assertEqual(shifted_shares([0.,1.],[.7,-.9]),[0.,1.])

    def test_goal_composition_uses_one_full_distribution_for_both_halves(self):
        ft=dict(fixture='f',metric='GOALS_FT',regime='XG',means=[2.,1.1])
        h=dict(fixture='f',metric='GOALS_1H',regime='XG',means=[99.,99.],shares=[.4,.5])
        a=compose_row(ft,h,[.3,-.2]);b=compose_row(ft,dict(h,metric='GOALS_2H',shares=[.6,.5]),[.3,-.2])
        profile={'GOALS_FT::XG':dict(scale=1.1,alpha=.3,draw_tilt=.2,btts_tilt=-.1)}
        full=distribution(ft,profile);first=distribution(a,profile);second=distribution(b,profile)
        np.testing.assert_allclose(np.asarray(first.means)+second.means,full.means,atol=1e-8)
        self.assertEqual(a['fullMeans'],ft['means']);self.assertLess(max(a['means']),3.)

    def test_venue_offsets_fit_only_provided_prior_counts(self):
        sample=[dict(shares=[.4,.6],first=[4.,1.],total=[5.,5.]) for _ in range(200)]
        calibrated=fit_offsets(sample);self.assertTrue(calibrated['fitted'])
        self.assertGreater(calibrated['offsets'][0],0);self.assertLess(calibrated['offsets'][1],0)
        self.assertLess(conditional_loss(sample,calibrated['offsets']),conditional_loss(sample,[0.,0.]))
        self.assertFalse(fit_offsets(sample[:10])['fitted'])

    def test_historical_half_targets_cannot_exceed_full_targets(self):
        ft=[dict(fixture='f',metric='GOALS_FT',actual=[0.,1.])]
        half=[dict(fixture='f',date=1.,metric='GOALS_1H',actual=[1.,1.],shares=[.5,.5])]
        with self.assertRaises(ValueError):records(ft,half)

    def test_locked_ft_uses_exact_saved_profile_without_refitting(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);saved={'GOALS_FT::XG':dict(scale=1.123456789,alpha=.23,draw_tilt=.04,btts_tilt=-.07),
                'CORNERS::XG':dict(scale=2.)}
            (path/'full_2026-10-01_parameters.json').write_text(json.dumps(saved))
            result=final_profile(SimpleNamespace(baseline=path,output=path),'full','2026-10-01','joint')
            self.assertEqual(result,{'GOALS_FT::XG':saved['GOALS_FT::XG']})

    def test_future_prediction_needs_no_actual_full_or_half_score(self):
        ft=dict(fixture='future',metric='GOALS_FT',regime='XG',means=[1.7,1.2])
        half=dict(fixture='future',metric='GOALS_1H',regime='XG',shares=[.4,.45])
        prediction=compose_row(ft,half,[.1,-.1])
        self.assertNotIn('actual',prediction)
        joint=distribution(prediction,{})
        self.assertAlmostEqual(float(joint.pmf.sum()),1.)

    def test_goal_ridge_features_exclude_same_day_and_post_cutoff_labels(self):
        day=86400
        def row(date,home,away,goals):
            return dict(_date=date,_home=home,_away=away,_home_group='L',_away_group='L',goals=goals)
        rows=[row(day+1,'a','b',[1,0]),row(day+2,'b','a',[2,1]),row(2*day+1,'a','b',[99,99])]
        metric=lambda r,m:r['goals'] if m=='GOALS_FT' else None
        model,x,y,columns,scales=prior_design(rows,2*day,metric)
        self.assertEqual(model.matches,2);self.assertEqual(len(y),4);self.assertEqual(y.max(),2)
        self.assertTrue(all(features['recent_for']==0 for features,_ in model.train['GOALS_FT']))

if __name__=='__main__':unittest.main()
