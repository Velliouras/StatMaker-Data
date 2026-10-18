import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'));sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_raw_contract_recovery as helpers
from betting_v2.card_settlement_bounds import count_bounds,settlement_bounds

class CardBoundTests(unittest.TestCase):
    def setUp(self):self.helper=helpers.CardReadinessTests()
    def test_exact_ready_cards_are_degenerate_intervals(self):
        b=self.helper.body();self.helper.card(b);r=count_bounds(b);self.assertTrue(r['usable']);self.assertEqual(r['lower'],r['upper']);self.assertEqual(r['lower'],[1,0])
    def test_late_card_keeps_both_inclusion_and_exclusion_possible(self):
        b=self.helper.body();self.helper.card(b,minute=90);r=count_bounds(b);self.assertTrue(r['usable']);self.assertEqual(r['lower'],[0,0]);self.assertEqual(r['upper'],[2,0])
    def test_unknown_appearance_and_incomplete_events_have_no_invented_bound(self):
        b=self.helper.body();self.helper.card(b,pid=None);self.assertFalse(count_bounds(b)['usable']);b=self.helper.body();b['statistics'][0]['statistics'][0]['value']=1;self.assertFalse(count_bounds(b)['usable'])
    def test_integer_line_can_include_win_push_loss_without_guess(self):
        r=settlement_bounds(['BET365_CARD_UNITS','TOTAL','OVER','',1.],2.,dict(usable=True,lower=[0,0],upper=[2,0]));self.assertEqual(set(r['states']),{'WIN','PUSH','LOSS'});self.assertEqual([r['profitLower'],r['profitUpper']],[-1.,1.])
    def test_ambiguous_count_can_have_unambiguous_market_result(self):
        for side,state in [('OVER','WIN'),('UNDER','LOSS')]:
            r=settlement_bounds(['BET365_CARD_UNITS','TOTAL',side,'',3.5],1.9,dict(usable=True,lower=[3,2],upper=[5,2]));self.assertTrue(r['unambiguous']);self.assertEqual(r['states'],[state])
    def test_team_contract_uses_its_own_bounds(self):
        r=settlement_bounds(['BET365_CARD_UNITS','TEAM','UNDER','AWAY',1.5],2.,dict(usable=True,lower=[3,0],upper=[5,1]));self.assertEqual(r['states'],['WIN'])
    def test_old_hy_plus_hr_target_is_never_silently_reused(self):
        with self.assertRaises(ValueError):settlement_bounds(['CARDS','TOTAL','OVER','',3.5],2.,dict(usable=True,lower=[1,1],upper=[1,1]))

if __name__=='__main__':unittest.main()
