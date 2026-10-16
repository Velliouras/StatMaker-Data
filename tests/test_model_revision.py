import copy
import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.reliable_goals import ReliableGoals,rolling_predictions
from betting_v2.selected_joint_calibration import apply_tilt,cell_mass,event_mask,fit_selected
from betting_v2.joint_count import JointCount
from betting_v2.validated_walk_forward import distribution
from betting_v2.research_model_revision import lower_probabilities
import materialize_betting_v2_direct as direct


def history():
    rows=[]
    for i in range(48):
        day=direct.timestamp('2026-01-01')+i*86400
        for j,(h,a) in enumerate([('a','b'),('c','d')]):
            if i%2:h,a=a,h
            rows.append(dict(_date=day+3600+j*3600,_home=h,_away=a,_home_group='L',_away_group='L',
                home_goals=(i+j)%4,away_goals=(i+2*j)%3,normalized_stats={'HxG':1.4,'AxG':1.1}))
    return rows


class ReliableGoalTests(unittest.TestCase):
    def test_sparse_features_pull_towards_prior_instead_of_raw_extreme(self):
        m=ReliableGoals();m.groups.update(a='L',b='L');m.priors['L','GOALS_FT',None]=[125.,125.,100.]
        m.history['a']['GOALS_FT']=[(8.,0.,True)];m.history['b']['GOALS_FT']=[(0.,8.,False)]
        f=m.features('a','b','GOALS_FT',True)
        self.assertAlmostEqual(f['recent_for'],(8+8*1.25)/9)
        self.assertAlmostEqual(f['recent_reliability'],1/9)
        self.assertEqual(f['xg_for_observed'],0.)
        self.assertEqual(f['xg_reliability'],0.)

    def test_future_results_cannot_change_training_design_or_prior_state(self):
        h=history();cutoff=direct.timestamp('2026-02-01')
        a=ReliableGoals();xa,ya,da,ta=a.design(h,cutoff,direct.metric_values)
        changed=copy.deepcopy(h)
        for r in changed:
            if r['_date']>=cutoff:r['home_goals']=100;r['normalized_stats']={'HxG':100,'AxG':0}
        b=ReliableGoals();xb,yb,db,tb=b.design(changed,cutoff,direct.metric_values)
        self.assertEqual((xa!=xb).nnz,0);np.testing.assert_array_equal(ya,yb)
        self.assertEqual(a.priors,b.priors);self.assertLess(da.max(),cutoff)

    def test_reliability_and_bootstrap_state_roundtrip(self):
        m=ReliableGoals({'bootstrapReplicas':2});cutoff=direct.timestamp('2026-02-01')
        design=m.design(history(),cutoff,direct.metric_values)
        info=m.fit_bank(design,recency=True,bootstrap=True,seed=7)
        self.assertEqual(len(m.fits),3);self.assertLess(info['effectiveFixtureWeights'],info['trainingMatches'])
        restored=ReliableGoals.restore(m.export())
        np.testing.assert_allclose(m.predict_bank('a','b'),restored.predict_bank('a','b'),rtol=0,atol=0)
        self.assertTrue(all(np.isfinite(m.predict_bank('a','b')).ravel()))

    def test_utc_day_predictions_do_not_use_same_day_results(self):
        h=history();cutoff=direct.timestamp('2026-02-01');end=cutoff+86400
        m=ReliableGoals({'bootstrapReplicas':0});design=m.design(h,cutoff,direct.metric_values)
        m.fit_bank(design,bootstrap=False);saved=m.export()
        first=rolling_predictions(m,h,cutoff,end,direct.metric_values)
        changed=copy.deepcopy(h)
        for r in changed:
            if cutoff<=r['_date']<end:r['home_goals']=9;r['away_goals']=8
        second=rolling_predictions(ReliableGoals.restore(saved),changed,cutoff,end,direct.metric_values)
        self.assertEqual([r['means'] for r in first],[r['means'] for r in second])
        self.assertTrue(all(r['featureCutoff']==cutoff for r in first))


class SelectedJointTests(unittest.TestCase):
    def test_identity_calibration_is_exact_and_all_probabilities_stay_coherent(self):
        j=JointCount.from_means(1.8,.9)
        self.assertIs(apply_tilt(j,[0,0,0,0]),j)
        changed=apply_tilt(j,[-.4,.2,-.3,-.1])
        self.assertAlmostEqual(changed.pmf.sum(),1.)
        self.assertAlmostEqual(sum(changed.evaluate(('GOALS_FT','RESULT',s,'',None))[0] for s in ('HOME','DRAW','AWAY')),1.)
        self.assertAlmostEqual(sum(changed.evaluate(('GOALS_FT','TOTAL',s,'',2.5))[0] for s in ('OVER','UNDER')),1.)
        cells=cell_mass(changed)
        for spec in [('GOALS_FT','RESULT','DRAW','',None),('GOALS_FT','TOTAL','OVER','',2.5)]:
            self.assertAlmostEqual(cells@event_mask(spec),changed.evaluate(spec)[0])

    def test_profile_tilt_is_applied_before_period_thinning(self):
        row=dict(metric='GOALS_FT',means=[1.8,.9],regime='ELO_NO_XG');profile={'_selected_joint':[-.4,.2,-.3,-.1]}
        full=distribution(row,profile)
        half=distribution(dict(row,metric='GOALS_1H',fullMeans=row['means'],shares=[.4,.45]),profile)
        np.testing.assert_allclose(full.thin(.4,.45).pmf,half.pmf,rtol=0,atol=0)
        np.testing.assert_allclose(distribution(row,{}).pmf,JointCount.from_means(*row['means']).pmf,rtol=0,atol=0)

    def test_small_prior_cohort_keeps_identity_with_explicit_reason(self):
        result=fit_selected([])
        self.assertFalse(result['fitted']);self.assertEqual(result['theta'],[0.]*4)

    def test_selected_fit_does_not_read_price_or_roi_and_reduces_overconfidence(self):
        base=cell_mass(JointCount.from_means(2.5,.6)).tolist()
        records=[dict(fixture=str(i),date=i*86400,spec=('GOALS_FT','RESULT','HOME','',None),
                      y=int(i%4==0),base=base,odd=2.,roi=999.) for i in range(80)]
        fitted=fit_selected(records);altered=copy.deepcopy(records)
        for r in altered:r['odd']=100.;r['roi']=-999.
        self.assertEqual(fitted,fit_selected(altered));self.assertTrue(fitted['fitted'])
        j=JointCount.from_means(2.5,.6);spec=records[0]['spec']
        self.assertLess(apply_tilt(j,fitted['theta']).evaluate(spec)[0],j.evaluate(spec)[0])

    def test_uncertainty_never_increases_point_probability(self):
        row=dict(metric='GOALS_FT',regime='ELO_NO_XG',means=[1.8,.9],bootstrapMeans=[[1.8+i*.1,.9] for i in range(12)])
        cal=dict(point={'theta':[0.]*4},replicas=[{'theta':[0.]*4} for _ in range(12)])
        spec=('GOALS_FT','RESULT','HOME','',None);lower,support=lower_probabilities(row,{},cal,spec,'HOME')
        self.assertLessEqual(lower,distribution(row,{}).evaluate(spec)[0]);self.assertLessEqual(support,1.)
        broken=copy.deepcopy(row);broken['bootstrapMeans']=broken['bootstrapMeans'][:-1]
        with self.assertRaises(ValueError):lower_probabilities(broken,{},cal,spec,None)


if __name__=='__main__':unittest.main()
