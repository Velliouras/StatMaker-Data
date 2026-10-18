import copy,math,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.research_gap_goals import eligible,conservative,proper_scores,goal_partitions,KINDS
from betting_v2.research_scope_expansion import select_scope

class GapGoalReplayTests(unittest.TestCase):
    def quote(self,side='OVER'):
        return dict(fixture='f',sourceReceipt='r',bookmaker='Bet365',providerEventId='1',observed=300000.,kickoff=400000.,
                    featureCutoff=172800.,marketUpdatedAtUTC=290000.,odd=1.9,spec=['GOALS_FT','TOTAL',side,'',2.5])

    def test_secondary_55_policy_is_separate_from_strong_and_keeps_odds_band(self):
        q=self.quote();v=dict(homeMean=1.5,awayMean=1.,scenarios={})
        self.assertIsNone(eligible(q,v,None,None,.55,0.,.6));self.assertIsNotNone(eligible(q,v,None,None,.55,0.,.55))
        for odd in (1.8,3.):self.assertIsNone(eligible(dict(q,odd=odd),v,None,None,.6,0.,.55))

    def test_favorite_scenario_floor_is_still_60_in_secondary_policy(self):
        class Full:
            def evaluate(self,spec):return (.7 if spec[2]=='HOME' else .2),0.
        v=dict(homeMean=1.5,awayMean=1.,scenarios={'HOME':dict(winProbability=.59)})
        self.assertIsNone(eligible(self.quote(),v,Full(),'HOME',.56,0.,.55))
        v['scenarios']['HOME']['winProbability']=.7
        self.assertIsNotNone(eligible(self.quote(),v,Full(),'HOME',.56,0.,.55))

    def test_rejected_point_winner_is_not_replaced_by_other_goal_scope(self):
        a=dict(self.quote(),p=.65,push=0.,ev=.2,confidence=.65,preference=0,_lower=dict(p=.53,push=0.,support=None))
        b=dict(a,p=.7,confidence=.7,preference=1,spec=['GOALS_FT','BTTS','YES','',None],_lower=dict(p=.65,push=0.,support=None))
        winners=select_scope([a,b]);self.assertEqual(winners,[a]);self.assertIsNone(conservative(winners[0],.55))

    def test_pushes_and_fixture_weights_are_scored_correctly(self):
        p={k:[.4,.2] for k in KINDS};r=dict(fixture='f',win=0.,push=1.,probabilities=p)
        a=proper_scores([r]);b=proper_scores([r]*10)
        for k in KINDS:
            self.assertAlmostEqual(a[k]['logLoss'],-math.log(.2));self.assertAlmostEqual(a[k]['logLoss'],b[k]['logLoss'])

    def test_partitions_reject_conflicts_and_mixed_numeric_identity(self):
        rows=[self.quote(),self.quote('UNDER')];self.assertEqual(len(goal_partitions(rows)[0]),1)
        conflict=dict(rows[0],odd=2.1);self.assertFalse(goal_partitions(rows+[conflict])[0])
        bad=dict(rows[1],providerEventId='2');self.assertFalse(goal_partitions([rows[0],bad])[0])

if __name__=='__main__':unittest.main()
