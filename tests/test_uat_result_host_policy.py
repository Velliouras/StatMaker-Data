import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from materialize_uat_direction_pattern_candidates import uat_result_premium_eligibility as approved

class UatResultHostPolicyTest(unittest.TestCase):
    def check(self, **kw):
        params=dict(sub_market_key='RESULT_1X2',odd=2.0,bookmaker_market=.50,
            model_probability=.59,modifier_profile='goals_ft',
            model_history_sample=12,bookmaker_history_sample=20,
            bookmaker_score=.65,market_source='no-vig',market_overround=1.07)
        params.update(kw)
        return approved(**params)
    def test_mature_real_fixture_model_can_provision_1x2_below_65(self):
        self.assertTrue(self.check())
    def test_market_probability_alone_cannot_promote_result(self):
        self.assertFalse(self.check(model_probability=None))
        self.assertFalse(self.check(modifier_profile=''))
    def test_65_percent_without_market_value_is_no_longer_enough(self):
        self.assertFalse(self.check(odd=1.80,bookmaker_market=.63,model_probability=.65))
    def test_current_tier_and_pattern_sample_required(self):
        self.assertFalse(self.check(model_history_sample=6))
        self.assertFalse(self.check(bookmaker_history_sample=5))
    def test_dedicated_conservative_validation_rejects_weak_edge(self):
        self.assertFalse(self.check(model_probability=.53))
    def test_double_chance_stale_overlapping_pool_never_receives_approval(self):
        self.assertFalse(self.check(sub_market_key='RESULT_DOUBLE_CHANCE',
            bookmaker_market=.28,model_probability=.53,market_overround=2.10))
    def test_no_model_free_pass_for_non_result_markets(self):
        self.assertFalse(self.check(sub_market_key='HOME_TEAM_TOTAL'))
    def test_longshot_beyond_global_value_first_cap_rejected(self):
        self.assertFalse(self.check(odd=5.01))
if __name__=='__main__':unittest.main()
