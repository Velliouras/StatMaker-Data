import sys,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/betting_v2'))
from betting_v2.chronological_stack import ChronologicalStack,PriorGoalBank,fit_prior_goals,initialize
from betting_v2.opponent_matchup import timestamp
from betting_v2.research_quotes import feature_cutoff,stats,assess,one_fixture_receipt
from betting_v2.exact_forecast_adapter import prepare
from betting_v2.validated_walk_forward import distribution
from materialize_betting_v2_direct import metric_values
from betting_v2.score_forward_research import settled

class ResearchIntegrityTest(unittest.TestCase):
    def test_missing_forward_result_remains_unscored(self):
        rows=[dict(fixture='future',observed=1.,kickoff=3.,spec=('GOALS_FT','RESULT','HOME','',None))]
        scored,missing=settled(rows,{},2.)
        self.assertEqual(scored,[]);self.assertEqual(missing,1)
        with self.assertRaises(ValueError):settled(rows,{},4.)
    def test_fixture_decision_never_mixes_bookmaker_packets(self):
        q=[dict(fixture='f',sourceReceipt='early',observed=1,bookmaker='a'),
           dict(fixture='f',sourceReceipt='late',observed=2,bookmaker='b')]
        self.assertEqual([v['bookmaker'] for v in one_fixture_receipt(q)],['a'])
        self.assertEqual([v['bookmaker'] for v in one_fixture_receipt(q,latest=True)],['b'])
    def test_prior_fit_excludes_current_month_targets(self):
        cutoff=timestamp('2026-08-01');rows=[dict(_date=cutoff-86400,_home='a',_away='b',_home_group='L',_away_group='L',home_goals=1,away_goals=0) for _ in range(200)]
        rows.append(dict(rows[0],_date=cutoff+3600,home_goals=99))
        def fit(x,y,columns,metric,alpha):
            self.assertEqual(len(y),400);self.assertEqual(y.max(),1);return SimpleNamespace(coef_=np.zeros(len(columns)))
        with patch('betting_v2.chronological_stack.fit_monotone_count',fit):result=fit_prior_goals(rows,cutoff,metric_values)
        self.assertLess(result['maxTrainingTimestamp'],cutoff)
    def test_zero_prior_strength_never_falls_back_to_final_model(self):
        model=ChronologicalStack.__new__(ChronologicalStack);initialize(model)
        model.models['GOALS_FT']=SimpleNamespace(coef_=np.array([100.]));model.columns['GOALS_FT']={'attack:a':0}
        f={'attack:a':1.,'defence:b':1.,'league_attack:L':1.,'league_defence:L':1.,'_prior_goal_strength':0.}
        self.assertEqual(model.count_strength_features(f)['goal_strength_gap'],0.)
    def test_bank_uses_month_start_and_reuses_fit(self):
        bank=PriorGoalBank([],metric_values);r=dict(_date=timestamp('2026-08-20'),_home='a',_away='b')
        with patch('betting_v2.chronological_stack.fit_prior_goals',return_value=dict(coldStart=True)) as fit:
            for _ in range(2):self.assertEqual(bank.strength(r,'a','b','L','L',True),0.)
        self.assertEqual(fit.call_count,1);self.assertEqual(fit.call_args.args[1],timestamp('2026-08-01'))
    def test_quote_features_have_at_least_twenty_four_hours_lag(self):
        for t in ('2026-10-12T00:01:00Z','2026-10-12T23:59:00Z'):
            now=timestamp(t);self.assertEqual(feature_cutoff(now),timestamp('2026-10-11'));self.assertGreaterEqual(now-feature_cutoff(now),86400)
    def test_small_cluster_sample_does_not_gain_precision_from_duplicate_bets(self):
        r=stats([dict(fixture='f',y=0,actualPush=1,odd=2.,p=.6)]*40)
        self.assertEqual(r['profitUnits'],0);self.assertEqual(r['fixtures'],1);self.assertIsNone(r['roi95'])
    def test_neutral_btts_only_changes_preference(self):
        row=dict(metric='GOALS_FT',regime='XG',means=[3.,.05]);pred=row,prepare(row,{}),distribution(row,{})
        q=dict(spec=('GOALS_FT','BTTS','NO','',None),odd=2.)
        a=assess(q,pred,{},'HOME','current_preference');b=assess(q,pred,{},'HOME','neutral_btts')
        self.assertEqual(a['preference'],0);self.assertEqual(b['preference'],1)
        for k in ('p','push','ev','confidence'):self.assertEqual(a[k],b[k])
