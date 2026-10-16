import sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/betting_v2'))
from betting_v2.research_regularization import (split_calibration,profile_mode,
    select_choices,common_rows,GROUPS)
from betting_v2.chronological_stack import ChronologicalStack
from betting_v2.validated_walk_forward import distribution
from betting_v2.score_forward_research import paired_comparisons
from betting_v2.research_event_grid import grid_probabilities,validation_events
from betting_v2.validated_walk_forward import grid,event_rows

class ChronologicalSelectionTest(unittest.TestCase):
    def test_profile_and_selection_labels_stay_strictly_before_test(self):
        day=86400;rows=[dict(date=v*day) for v in (0,19,20,29,30,100)]
        boundary,earlier,later=split_calibration(rows,0,30*day)
        self.assertEqual(boundary,20*day)
        self.assertEqual([r['date']/day for r in earlier],[0,19])
        self.assertEqual([r['date']/day for r in later],[20,29])
        changed=[dict(r,date=1000*day) if r['date']>=30*day else r for r in rows]
        self.assertEqual(split_calibration(changed,0,30*day),(boundary,earlier,later))

    def test_candidates_have_identical_evaluation_cohort(self):
        shared=dict(fixture='a',metric='SOT')
        values=common_rows(dict(a=[shared,dict(fixture='b',metric='SOT')],b=[shared]))
        self.assertEqual(values,dict(a=[shared],b=[shared]))

    def test_goal_periods_cannot_choose_incoherent_different_candidates(self):
        def events(q):
            return [dict(fixture='a',metric=m,market='TOTAL|OVER||0.5',q=q[m],y=1)
                for metrics in GROUPS.values() for m in metrics]
        qa={m:.9 for metrics in GROUPS.values() for m in metrics}
        qb=dict(qa,GOALS_FT=.99,GOALS_1H=.1,GOALS_2H=.1)
        choices,_,_=select_choices({('a','joint'):events(qa),('b','joint'):events(qb)})
        self.assertEqual(choices['GOALS']['candidate'],'a')
        self.assertEqual(set(choices),set(GROUPS))

    def test_unshared_easy_validation_events_cannot_bias_selection(self):
        def events(name,q):
            return [dict(fixture=name,metric=m,market='x',q=q,y=1)
                for metrics in GROUPS.values() for m in metrics]
        a=events('shared',.9);b=events('shared',.8)+events('unshared',1.)*100
        choices,_,n=select_choices({('a','joint'):a,('b','joint'):b})
        self.assertTrue(all(c['candidate']=='a' for c in choices.values()))
        self.assertEqual(n,9)

    def test_shrunk_profile_preserves_complements_and_goal_period_means(self):
        profile={'GOALS_FT::XG':dict(scale=1.44,alpha=.3,draw_tilt=.2,btts_tilt=-.1)}
        row=dict(metric='GOALS_FT',regime='XG',means=[1.6,1.2])
        shrunk=profile_mode(profile,'half_shrink');joint=distribution(row,shrunk)
        self.assertAlmostEqual(shrunk['GOALS_FT::XG']['scale'],1.2)
        over=joint.evaluate(('GOALS_FT','TOTAL','OVER','',2.5))[0]
        under=joint.evaluate(('GOALS_FT','TOTAL','UNDER','',2.5))[0]
        self.assertAlmostEqual(over+under,1.)
        halves=[distribution(dict(row,metric=m,fullMeans=row['means'],shares=s),shrunk)
            for m,s in [('GOALS_1H',[.4,.45]),('GOALS_2H',[.6,.55])]]
        np.testing.assert_allclose(np.asarray(halves[0].means)+halves[1].means,joint.means,atol=1e-8)

    def test_invalid_ridge_rejected_before_any_training(self):
        for factor in (0.,-1.,float('inf'),float('nan')):
            with self.assertRaises(ValueError):
                ChronologicalStack([],0,None,bank=None,ridge_factor=factor)

    def test_prospective_scorer_pairs_any_frozen_candidate_and_checks_labels(self):
        b=dict(fixture='a',winProbability=.6,pushProbability=.2,y=1)
        c=dict(b,winProbability=.7)
        reports=paired_comparisons(dict(full={'key':b},prior_window_selected={'key':c}))
        self.assertEqual(reports['prior_window_selected']['n'],1)
        self.assertAlmostEqual(reports['prior_window_selected']['baseline']['brier'],.0625)
        with self.assertRaises(ValueError):
            paired_comparisons(dict(full={'key':b},prior_window_selected={'key':dict(c,y=0)}))
        self.assertEqual(paired_comparisons(dict(full={},prior_window_selected={})),{'prior_window_selected':None})

    def test_fast_grid_matches_joint_masks_across_all_market_families(self):
        from betting_v2.joint_count import JointCount
        for metric in [m for metrics in GROUPS.values() for m in metrics]:
            specs=grid(metric)
            for alpha in (0.,.3):
                joint=JointCount.from_means(2.4,1.1,alpha=alpha,draw_tilt=.3,btts_tilt=-.2)
                np.testing.assert_allclose(grid_probabilities(joint,specs),
                    [joint.evaluate(s) for s in specs],rtol=1e-12,atol=1e-14)

    def test_fast_validation_preserves_labels_push_exclusions_and_probabilities(self):
        rows=[dict(fixture='a',metric=m,regime='XG',means=[1.3,1.8],actual=[1,1])
            for m in ['GOALS_FT','CORNERS','SHOTS','FOULS']]
        slow=event_rows(rows,{});fast=validation_events(rows,{})
        self.assertEqual([(r['market'],r['y']) for r in slow],[(r['market'],r['y']) for r in fast])
        np.testing.assert_allclose([r['q'] for r in slow],[r['q'] for r in fast],rtol=1e-12,atol=1e-14)

if __name__=='__main__':unittest.main()
