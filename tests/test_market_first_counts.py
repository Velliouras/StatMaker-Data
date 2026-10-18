import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.research_market_first_counts import retain,probability_scores
from betting_v2.research_context_market import conservative_winner

class MarketPacketTests(unittest.TestCase):
    def row(self,metric,receipt,clock,side='OVER'):
        return dict(fixture='f',spec=[metric,'TOTAL',side,'',9.5],observed=clock,sourceReceipt=receipt,featureCutoff=0.,kickoff=500000.,providerEventId='123',bookmaker='b',_favourite='HOME',_sameReceiptResultOdds=dict(HOME=1.6,DRAW=4.,AWAY=5.))
    def test_market_first_recovers_later_appearing_metric_without_replacing_other_metric(self):
        chosen={};retain(chosen,[self.row('CORNERS','a',100000.)]);retain(chosen,[self.row('CORNERS','b',200000.),self.row('SOT','b',200000.)]);self.assertEqual(chosen['f','CORNERS']['rank'][1],'a');self.assertEqual(chosen['f','SOT']['rank'][1],'b')
    def test_latest_keeps_each_metrics_last_actual_receipt(self):
        chosen={};retain(chosen,[self.row('CORNERS','a',100000.),self.row('SOT','a',100000.)],True);retain(chosen,[self.row('CORNERS','b',200000.)],True);self.assertEqual(chosen['f','SOT']['rank'][1],'a');self.assertEqual(chosen['f','CORNERS']['rank'][1],'b')
    def test_sides_and_books_stay_in_one_receipt_with_original_favorite_evidence(self):
        rows=[self.row('SOT','a',100000.),self.row('SOT','a',100000.,'UNDER')];rows.append(dict(rows[0],bookmaker='another',_favourite=None));c={};retain(c,rows);self.assertEqual(c['f','SOT']['rows'],rows)
        self.assertEqual(c['f','SOT']['rows'][0]['_sameReceiptResultOdds']['HOME'],1.6)
    def test_mixed_clocks_are_rejected_instead_of_merged(self):
        for change in [dict(sourceReceipt='b'),dict(featureCutoff=86400.),dict(providerEventId='124'),dict(kickoff=500001.)]:
            with self.assertRaises(ValueError):retain({},[self.row('SOT','a',100000.),dict(self.row('SOT','a',100000.,'UNDER'),**change)])
    def test_half_and_other_metrics_are_outside_count_total_team_experiment(self):
        c={};retain(c,[self.row('GOALS_FT','a',100000.),dict(self.row('CORNERS','a',100000.),spec=['CORNERS','RESULT','HOME','',None])]);self.assertFalse(c)
    def test_proper_scores_weight_each_fixture_once_and_keep_push_state(self):
        a=dict(fixture='a',y=1.,push=0.,probabilities={k:[.6,.1] for k in ('control','coupled')});b=dict(fixture='b',y=0.,push=1.,probabilities={k:[.2,.5] for k in ('control','coupled')});self.assertAlmostEqual(probability_scores([a,b])['coupled']['logLoss'],probability_scores([a,a,a,b])['coupled']['logLoss'])
    def test_fixed_winner_conservative_failure_is_an_abstention(self):
        q=dict(spec=['SOT','TEAM','OVER','HOME',4.5],p=.65,push=0.,odd=2.,_lower=dict(p=.59,push=0.,support=.7));self.assertIsNone(conservative_winner(q))

if __name__=='__main__':unittest.main()
