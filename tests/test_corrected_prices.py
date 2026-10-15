import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/betting_v2'))
from betting_v2.replay_corrected_prices import one_snapshot, policy

class PriceReplayTest(unittest.TestCase):
    def test_never_combines_different_quote_times(self):
        def item(receipt,time,book='a'):
            return dict(fixture='f',bookmaker=book,observed=time,sourceReceipt=receipt)
        quotes={1:item('first','04:00'),2:item('later','05:00'),
                3:item('first','04:00'),4:item('other','05:00','b')}
        self.assertEqual(set(one_snapshot(quotes)),{1,3,4})

    def test_odds_bounds_are_strict(self):
        spec=('GOALS_FT','TOTAL','OVER','',.5)
        rows={'GOALS_FT':dict(metric='GOALS_FT',regime='XG',means=[2.,1.])}
        for odd in (1.8,3.):
            result,reason=policy(dict(spec=spec,odd=odd),rows,{},None,True)
            self.assertIsNone(result);self.assertEqual(reason,'ODDS_OUTSIDE_RANGE')
