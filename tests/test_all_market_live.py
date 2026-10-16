import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_reliable_counts import counts_history
from betting_v2.reliable_counts import METRICS,ReliableCounts,metric_values
from betting_v2.reliable_goals import ReliableGoals
from betting_v2.chronological_stack import PriorGoalBank
from betting_v2.live_revised import RevisedModel
from betting_v2.live_factorized import FactorizedModel
from betting_v2.live_counts import AllMarketModel
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.opponent_matchup import timestamp


class AllMarketLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows=counts_history();cutoff=timestamp('2026-02-01');bank=PriorGoalBank(rows,metric_values)
        cls.counts={};cls.profiles={}
        for metric in METRICS:
            m=ReliableCounts(dict(metric=metric,bootstrapReplicas=2))
            d=m.design(rows,cutoff,metric_values,bank=bank);m.fit_bank(d,seed=9)
            cls.counts[metric]=ReliableCounts.restore(m.export())
            cls.profiles[metric]=[{metric+'::XG':dict(scale=s,alpha=.05),metric+'::ELO_NO_XG':dict(scale=s,alpha=.05)} for s in (1.1,1.,1.2)]
        ft=ReliableGoals(dict(bootstrapReplicas=2));d=ft.design(rows,cutoff,metric_values);ft.fit_bank(d,seed=7)
        class Period:
            def predict(self,h,a,metric):
                p=ft.predict(h,a,'GOALS_FT')
                share={'GOALS_FT':1.,'GOALS_1H':.4,'GOALS_2H':.6}[metric]
                return (p[0]*share,p[1]*share,*p[2:])
        class OldCounts:
            history=ft.history;xg=ft.xg
            def predict(self,h,a,metric):return (1.,1.,20,20,100)
        old=FactorizedModel(ft,Period(),OldCounts(),[0.,0.])
        cal=dict(replicas=[dict(theta=[0.]*4)]*2)
        cls.base=RevisedModel(ft,old,{},cal,.1)
        cls.model=AllMarketModel(cls.base,cls.counts,cls.profiles)
        cls.model.revision_profile={k:v for p in cls.profiles.values() for k,v in p[0].items()}

    def test_all_count_cores_replace_old_weights_while_goal_predictions_match(self):
        for metric in METRICS:
            self.assertEqual(self.model.predict('a','b',metric),self.counts[metric].predict('a','b',metric))
            self.assertNotEqual(self.model.predict('a','b',metric)[:2],(1.,1.))
        for metric in ('GOALS_FT','GOALS_1H','GOALS_2H'):
            self.assertEqual(self.model.predict('a','b',metric),self.base.predict('a','b',metric))

    def test_each_metric_uses_own_prior_profile_and_conservative_ev(self):
        with patch('betting_v2.reliable_goals.weighted_fit',side_effect=AssertionError('Live estimator fit')):
            for metric in METRICS:
                spec=(metric,'TOTAL','OVER','',.5 if metric=='RED_CARDS' else 4.5)
                row,p,prepared=self.model.prepared_bank('a','b',metric)[0]
                expected=forecast(row,self.model.revision_profile,spec)
                point=forecast(row,p,spec,prepared);bound=self.model.bounds('a','b',spec,2.2)
                self.assertAlmostEqual(point['winProbability'],expected['winProbability'])
                self.assertLessEqual(bound['win'],point['winProbability'])
                self.assertLessEqual(bound['push'],point['pushProbability'])
                self.assertAlmostEqual(bound['ev'],bound['win']*2.2+bound['push']-1)

    def test_corner_band_and_integer_push_have_exact_bounds(self):
        for scope,line in [('RANGE','8:12'),('TOTAL',10.)]:
            spec=('CORNERS',scope,'OVER' if scope=='TOTAL' else 'YES','',line)
            row,p,prepared=self.model.prepared_bank('a','b','CORNERS')[0]
            point=forecast(row,p,spec,prepared);bound=self.model.bounds('a','b',spec,2.1)
            self.assertLessEqual(bound['win'],point['winProbability'])
            self.assertLessEqual(bound['push'],point['pushProbability'])

if __name__=='__main__':unittest.main()
