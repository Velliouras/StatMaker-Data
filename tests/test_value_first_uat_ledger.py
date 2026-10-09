"""UAT-only Value-First canonical selector checks; PROD remains on its existing engine."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import materialize_canonical_recommendation_ledger as m

def sample(**changes):
    row=dict(
        selection_odd=2.50, bm_market_probability=0.40,
        bm_posterior_probability=0.50, bm_sample_reliability=0.85,
        selection_score=0.82, strict_hit_rate=0.75,
        opponent_model_probability=0.50, value_tier='STRONG_VALUE',
        identity_team_side='HOME', simulation_probability=None
    )
    row.update(changes)
    return row

class ValueFirstLedgerUatTest(unittest.TestCase):
    def rank(self, **changes):
        return m._value_first_uat_rank(sample(**changes),True,False,'HOME')

    def test_two_point_five_not_blocked_by_old_56_percent_floor(self):
        self.assertIsNotNone(self.rank())

    def test_nine_to_one_is_rejected_even_if_positive_edge(self):
        self.assertIsNone(self.rank(selection_odd=9.0, bm_market_probability=0.11,
                                    bm_posterior_probability=0.19))

    def test_short_odd_with_no_real_edge_is_rejected(self):
        self.assertIsNone(self.rank(selection_odd=1.60,
                                    bm_market_probability=0.625,
                                    bm_posterior_probability=0.64))

    def test_old_tier_does_not_shadow_actual_positive_value(self):
        self.assertIsNotNone(self.rank(value_tier='VALUE'))

    def test_favorite_is_a_bounded_tie_breaker(self):
        h=m._value_first_uat_rank(sample(identity_team_side='HOME'),False,False,'HOME')
        a=m._value_first_uat_rank(sample(identity_team_side='AWAY'),False,False,'HOME')
        self.assertGreater(h[0],a[0])

    def test_favorite_never_overrides_negative_edge(self):
        self.assertIsNone(self.rank(bm_posterior_probability=0.39))

    def test_low_simulation_probability_is_not_a_flat_veto(self):
        self.assertIsNotNone(self.rank(simulation_probability=0.45))

    def test_many_families_one_market_per_family(self):
        entries=[
            dict(competition_id='D',match_key='m',market_family='Goals',_hybrid_rank=(0.70,)),
            dict(competition_id='D',match_key='m',market_family='Goals',_hybrid_rank=(0.80,)),
            dict(competition_id='D',match_key='m',market_family='Corners',_hybrid_rank=(0.72,)),
        ]
        selected=m._best_per_match_market_family(entries)
        self.assertEqual(len(selected),2)
        self.assertEqual({r['market_family'] for r in selected},{'Goals','Corners'})
        self.assertEqual(next(r['_hybrid_rank'][0] for r in selected if r['market_family']=='Goals'),0.80)

    def test_minimum_odd_can_only_hide_canonical_selections(self):
        selected=[
            dict(selection_odd=1.60, market_family='Corners'),
            dict(selection_odd=1.85, market_family='Goals'),
            dict(selection_odd=2.50, market_family='Cards'),
        ]
        low=[r for r in selected if r['selection_odd']>=1.50]
        high=[r for r in selected if r['selection_odd']>=1.80]
        self.assertTrue({r['market_family'] for r in high}.issubset(
            {r['market_family'] for r in low}))

if __name__=='__main__':
    unittest.main()
