import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import audit_value_first_market_gates as mod

class MarketFunnelRulesTest(unittest.TestCase):
    def row(self,**kw):
        x=dict(sub='RESULT_1X2',odd=1.8,market=.55,posterior=.65,
            model=.63,required=1,source='no-vig',overround=1.06,candidate=1,premium=0)
        x.update(kw)
        return x
    def test_stale_dc_book_is_200_percent_not_a_value_signal(self):
        x=mod.row_flags(self.row(sub='RESULT_DOUBLE_CHANCE',market=.25,
            overround=2.12,model=.31))
        self.assertTrue(x['stale_dc'])
        self.assertTrue(x['old_premium_zero'])
    def test_1x2_positive_fixture_value_is_shadow_not_premium(self):
        x=mod.row_flags(self.row())
        self.assertTrue(x['fixture_value'])
        self.assertTrue(x['old_premium_zero'])
    def test_missing_model_is_detected_independently_of_posterior(self):
        x=mod.row_flags(self.row(model=None))
        self.assertTrue(x['required_missing'])
        self.assertTrue(x['historical_value'])
        self.assertFalse(x['fixture_value'])

if __name__=='__main__':unittest.main()
