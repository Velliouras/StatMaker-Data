import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import numpy as np
from betting_v2.joint_count import JointCount
from betting_v2.research_app_markets import ResearchJoint,outcome_extended,mapped_app_spec,parsed_range,prepare_extended,range_spec

class ExistingAppMarketsTest(unittest.TestCase):
    def setUp(self):self.base=JointCount.from_means(1.8,1.1,alpha=.2,draw_tilt=.2,btts_tilt=-.1)
    def spec(self,scope,side='',team='',line=None):return ('GOALS_FT',scope,side,team,line)
    def test_exact_scores_partition_the_same_calibrated_ft_distribution(self):
        d=ResearchJoint(self.base);nh,na=self.base.pmf.shape
        value=sum(d.evaluate(self.spec('EXACT','SCORE',line=f'{h}:{a}'))[0] for h in range(nh) for a in range(na))
        self.assertAlmostEqual(value,1.)
    def test_ranges_partition_and_have_no_integer_push(self):
        d=ResearchJoint(self.base)
        specs=[self.spec('RANGE','IN',line=s) for s in ['0:1','2:3','4:INF']]
        self.assertAlmostEqual(sum(d.evaluate(s)[0] for s in specs),1.)
        self.assertEqual(outcome_extended(2,1,specs[1]),(1.,0.))
        self.assertEqual(outcome_extended(2,1,('GOALS_FT','TOTAL','OVER','',3.)),(0.,1.))
    def test_all_margin_buckets_include_only_one_draw_category(self):
        d=ResearchJoint(self.base);specs=[self.spec('MARGIN',s,line=None) for s in ['NO_GOAL','SCORE_DRAW']]
        specs += [self.spec('MARGIN',side,line=line) for side in ['HOME','AWAY'] for line in ['1:1','2:2','3:3','4:INF']]
        self.assertAlmostEqual(sum(d.evaluate(s)[0] for s in specs),1.)
        self.assertEqual(outcome_extended(0,0,self.spec('MARGIN','NO_GOAL')),(1.,0.))
        self.assertEqual(outcome_extended(1,1,self.spec('MARGIN','NO_GOAL')),(0.,0.))
    def test_htft_rows_and_columns_match_the_existing_marginals(self):
        shares=[.4,.55];d=ResearchJoint(self.base,shares);sides=['HOME','DRAW','AWAY']
        values=np.array([[d.evaluate(self.spec('HTFT',f'{h}_{f}'))[0] for f in sides] for h in sides])
        self.assertAlmostEqual(values.sum(),1.)
        np.testing.assert_allclose(values.sum(axis=0),[self.base.evaluate(('GOALS_FT','RESULT',s,'',None))[0] for s in sides],atol=1e-12)
        half=self.base.thin(*shares)
        np.testing.assert_allclose(values.sum(axis=1),[half.evaluate(('GOALS_1H','RESULT',s,'',None))[0] for s in sides],atol=1e-12)
    def test_htft_uses_actual_half_score_for_settlement(self):
        spec=self.spec('HTFT','AWAY_HOME')
        self.assertEqual(outcome_extended(2,1,spec,first=(0,1)),(1.,0.))
        self.assertEqual(outcome_extended(2,1,spec,first=(1,0)),(0.,0.))
        with self.assertRaises(ValueError):outcome_extended(2,1,spec,first=(3,0))
    def test_both_halves_handle_zero_goals_one_goal_and_deterministic_shares(self):
        for home,away in [(0,0),(1,0),(0,1)]:
            mass=np.zeros((home+1,away+1));mass[home,away]=1
            self.assertEqual(ResearchJoint(JointCount(mass),[.4,.5]).evaluate(self.spec('BOTH_HALVES','YES')),(0.,0.))
        d=ResearchJoint(self.base,[.4,.5])
        for team in ['', 'HOME','AWAY']:
            self.assertAlmostEqual(d.evaluate(self.spec('BOTH_HALVES','YES',team))[0]+d.evaluate(self.spec('BOTH_HALVES','NO',team))[0],1.)
        self.assertEqual(outcome_extended(2,1,self.spec('BOTH_HALVES','YES','HOME'),first=(1,0)),(1.,0.))
    def test_combined_events_partition_btts_and_refuse_unknown_integer_void_rules(self):
        d=ResearchJoint(self.base)
        specs=[self.spec('BTTS_TOTAL',f'{s}_{v}',line=2.5) for s in ['YES','NO'] for v in ['OVER','UNDER']]
        self.assertAlmostEqual(sum(d.evaluate(s)[0] for s in specs),1.)
        with self.assertRaises(ValueError):d.evaluate(self.spec('BTTS_TOTAL','YES_OVER',line=3.))
    def test_parity_is_complementary(self):
        d=ResearchJoint(self.base)
        self.assertAlmostEqual(sum(d.evaluate(self.spec('PARITY',s))[0] for s in ['ODD','EVEN']),1.)
    def test_real_receipt_labels_preserve_score_margin_and_range_semantics(self):
        def parse(name,selection):return mapped_app_spec(dict(providerMarketName=name,selection=selection),{},lambda *_:None)
        self.assertEqual(parse('Correct Score','2-1'),self.spec('EXACT','SCORE',line='2:1'))
        self.assertEqual(parse('Winning Margin','4+ (2)'),self.spec('MARGIN','AWAY',line='4:INF'))
        self.assertEqual(parse('Number of Goals In Match','2 or 3 goals'),self.spec('RANGE','IN',line='2:3'))
        self.assertEqual(parsed_range('Under 2 goals'),'0:1');self.assertEqual(parsed_range('Over 3 goals'),'4:INF')
        self.assertEqual(parse('ML HT','HOME'),('GOALS_1H','RESULT','HOME','',None))
    def test_player_multi_scorer_prices_never_reach_the_goal_fallback(self):
        def bad(*_):raise AssertionError('Player prop reached match parser')
        for name in ['To Score 2+ Goals','To Score 3+ Goals']:
            self.assertIsNone(mapped_app_spec(dict(providerMarketName=name,selection='OVER'),{},bad))
    def test_clean_sheet_uses_opponent_goals_and_period_btts_stays_in_its_period(self):
        def parse(name,selection):return mapped_app_spec(dict(providerMarketName=name,selection=selection),{},lambda *_:None)
        self.assertEqual(parse('Clean Sheet Home','Yes'),('GOALS_FT','TEAM','UNDER','AWAY',.5))
        self.assertEqual(parse('Both Teams To Score HT','No'),('GOALS_1H','BTTS','NO','',None))
        self.assertEqual(parse('Draw No Bet 2H','Away'),('GOALS_2H','DNB','AWAY','',None))
    def test_extended_preparation_preserves_old_probabilities_and_scenarios(self):
        from betting_v2.exact_forecast_adapter import prepare,forecast
        row=dict(metric='GOALS_1H',regime='XG',means=[.8,.5],fullMeans=[2.,1.],shares=[.4,.5])
        profile={'GOALS_FT::XG':dict(scale=1.1,alpha=.2,draw_tilt=.1,btts_tilt=-.1)}
        for spec in [('GOALS_1H','RESULT','HOME','',None),('GOALS_1H','TOTAL','OVER','',1.5)]:
            self.assertEqual(forecast(row,profile,spec,prepare(row,profile)),forecast(row,profile,spec,prepare_extended(row,profile)))
    def test_unbounded_goal_ranges_preserve_equivalent_team_total_policy_semantics(self):
        d=ResearchJoint(self.base)
        for team in ['', 'HOME','AWAY']:
            for line in ['0:0','0:2','2:INF']:
                normalized=range_spec('GOALS_FT',team,line)
                self.assertEqual(d.evaluate(normalized),d.evaluate(self.spec('RANGE','IN',team,line)))
if __name__=='__main__':unittest.main()
