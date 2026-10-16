import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.market_calibration import references,fit_weight,pooled_probability,select_ev


class MarketCalibrationTests(unittest.TestCase):
    def test_complete_other_book_is_used_and_own_book_alias_is_not_independent(self):
        def row(book,side,odd):return dict(fixture='f',sourceReceipt='r',spec=['CORNERS','TOTAL',side,'',9.5],bookmaker=book,odd=odd)
        rows=[row('Bet365','OVER',2.),row('Bet365','UNDER',1.8),row('Bet365 (no latency)','OVER',2.1),row('Bet365 (no latency)','UNDER',1.7),row('Unibet','OVER',2.2),row('Unibet','UNDER',1.7)]
        refs,_=references(rows)
        self.assertEqual(refs[0]['books'],['Unibet']);self.assertTrue(refs[0]['independent'])
        self.assertAlmostEqual(refs[0]['q'],(1/2.2)/(1/2.2+1/1.7))
        refs,_=references(rows[:4]);self.assertFalse(refs[0]['independent'])

    def test_incomplete_or_conflicting_pairs_never_invent_reference(self):
        base=dict(fixture='f',sourceReceipt='r',spec=['SOT','TOTAL','OVER','',8.5],bookmaker='A',odd=2.)
        self.assertFalse(references([base])[0])
        self.assertFalse(references([base,dict(base,odd=2.2),dict(base,spec=['SOT','TOTAL','UNDER','',8.5],odd=1.9)])[0])

    def test_weight_learns_prior_probabilities_without_prices_and_rejects_future_labels(self):
        rows=[dict(fixture=str(i),kickoff=i,statConditional=.75,marketConditional=.5,y=i%2) for i in range(100)]
        first=fit_weight(rows,100);self.assertEqual(first['weight'],0.)
        altered=[dict(r,odd=100.,ev=999.,profit=999.) for r in rows]
        self.assertEqual(first,fit_weight(altered,100))
        with self.assertRaises(ValueError):fit_weight(rows,99)
        good=[dict(r,statConditional=.9,y=1.) for r in rows]
        self.assertGreater(fit_weight(good,100)['weight'],.9)

    def test_push_is_retained_and_ev_does_not_treat_push_as_loss(self):
        p=pooled_probability(.5,.2,.5,.5)
        self.assertAlmostEqual(p,.45);self.assertAlmostEqual(p*2+.2-1,.1)

    def test_favourite_cannot_override_a_better_ev_and_duplicate_family(self):
        base=dict(fixture='f',spec=['CORNERS','TEAM','OVER','HOME',4.5],bookmaker='A',p=.62,ev=.12,preference=0)
        better=dict(base,spec=['CORNERS','TEAM','UNDER','AWAY',4.5],p=.7,ev=.3,preference=2)
        self.assertEqual(select_ev([base,better]),[better])

if __name__=='__main__':unittest.main()
