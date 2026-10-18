import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.settlement_request_plan import build,validate_cached_grades

class SettlementPlanTests(unittest.TestCase):
    def quote(self,event='123',side='OVER',market='Bookings Totals',line=4.5):
        return dict(spec=['BET365_CARD_UNITS','TOTAL',side,'',line],providerEventId=event,sourceMarket=market,fixture='f'+event,kickoff=500000.,observed=300000.,sourceReceipt='r',sourceSha256='a'*64,bookmaker='Bet365')
    def test_receipt_duplicates_and_opposite_sides_make_one_observed_query(self):
        rows=[self.quote(),self.quote(side='UNDER'),dict(self.quote(),sourceReceipt='later',observed=400000.)];p=build(rows);self.assertEqual(len(p['selections']),1);self.assertEqual(p['selections'][0]['side'],'over');self.assertFalse(p['summary']['executed'])
    def test_under_only_remains_an_observed_under_request(self):
        p=build([self.quote(side='UNDER')]);self.assertEqual(p['selections'][0]['side'],'under')
    def test_unknown_alias_is_not_silently_renamed(self):
        p=build([self.quote(market='Number of Cards In Match')]);self.assertFalse(p['selections']);self.assertTrue(p['summary']['excluded'])
    def test_documented_batch_cap_and_determinism(self):
        rows=[self.quote(str(i)) for i in range(101)];p=build(rows);self.assertEqual([len(b['selections']) for b in p['batches']],[50,50,1]);self.assertEqual(p,build(list(reversed(rows))))
    def test_wrong_fixture_identity_and_invalid_line_fail(self):
        with self.assertRaises(ValueError):build([self.quote(),dict(self.quote(),fixture='other')])
        with self.assertRaises(ValueError):build([self.quote(line=4.25)])
    def test_only_final_won_lost_and_push_can_supply_outcomes(self):
        p=build([self.quote()]);ref=p['selections'][0]['ref'];r=validate_cached_grades(p,[dict(grades=[dict(ref=ref,result='won',final=False)])]);self.assertEqual(r['coverage'],{'notFinal':1});r=validate_cached_grades(p,[dict(grades=[dict(ref=ref,result='void',reason='push',final=True)])]);self.assertEqual(r['usable'][ref]['oppositeState'],'PUSH')
    def test_ungradeable_missing_and_cancelled_are_not_losses(self):
        p=build([self.quote()]);ref=p['selections'][0]['ref']
        for g,key in [(dict(ref=ref,result='ungradeable',final=True),'ungradeable'),(dict(ref=ref,result='void',reason='event_cancelled',final=True),'voidNonPush')]:self.assertFalse(validate_cached_grades(p,[dict(grades=[g])])['usable'])
        self.assertEqual(validate_cached_grades(p,[])['coverage'],{'missing':1})
    def test_unknown_and_conflicting_grade_cannot_overwrite_a_cached_result(self):
        p=build([self.quote()]);ref=p['selections'][0]['ref']
        with self.assertRaises(ValueError):validate_cached_grades(p,[dict(grades=[dict(ref='other',result='won',final=True)])])
        with self.assertRaises(ValueError):validate_cached_grades(p,[dict(grades=[dict(ref=ref,result='won',final=True),dict(ref=ref,result='lost',final=True)])])

if __name__=='__main__':unittest.main()
