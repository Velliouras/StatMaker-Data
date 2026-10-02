import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import materialize_canonical_recommendation_ledger as target


def row(**overrides):
    base = {
        "selection_odd": 1.83,
        "bm_market_probability": 0.50,
        "bm_posterior_probability": 0.68,
        "bm_sample_reliability": 0.89,
        "opponent_model_probability": None,
        "selection_score": 0.78,
        "strict_hit_rate": 0.85,
    }
    base.update(overrides)
    return base


class HybridCanonicalLedgerSelectorTest(unittest.TestCase):
    def test_extreme_longshot_is_rejected(self):
        self.assertIsNone(
            target._hybrid_rank(
                row(
                    selection_odd=8.0,
                    bm_market_probability=0.12,
                    bm_posterior_probability=0.58,
                    opponent_model_probability=0.60,
                ),
                market_preferred=False,
                three_way_result=True,
            )
        )

    def test_three_way_underdog_without_fixture_model_is_rejected(self):
        self.assertIsNone(
            target._hybrid_rank(
                row(
                    selection_odd=3.20,
                    bm_market_probability=0.29,
                    bm_posterior_probability=0.48,
                    opponent_model_probability=None,
                ),
                market_preferred=False,
                three_way_result=True,
            )
        )

    def test_ordinary_negative_edge_pick_is_not_strong(self):
        # Strong v2 never uses Developing as a home for a negative-edge/negative-EV pick.
        ranked = target._hybrid_rank(
            row(
                selection_odd=1.57,
                bm_market_probability=0.584,
                bm_posterior_probability=0.557,
                opponent_model_probability=None,
                bm_sample_reliability=0.775,
                strict_hit_rate=0.60,
            ),
            market_preferred=True,
            three_way_result=False,
        )
        self.assertIsNone(ranked)

    def test_positive_value_mature_candidate_passes_strong_core(self):
        ranked = target._hybrid_rank(
            row(
                selection_odd=1.83,
                bm_market_probability=0.50,
                bm_posterior_probability=0.681,
                opponent_model_probability=None,
                bm_sample_reliability=0.892,
                selection_score=0.78,
                strict_hit_rate=0.846,
            ),
            market_preferred=True,
            three_way_result=False,
        )
        self.assertIsNotNone(ranked)

    def test_probability_backed_team_market_is_ranked(self):
        ranked = target._hybrid_rank(
            row(
                selection_odd=1.83,
                bm_market_probability=0.50,
                bm_posterior_probability=0.681,
                bm_sample_reliability=0.892,
                strict_hit_rate=0.846,
            ),
            market_preferred=True,
            three_way_result=False,
        )
        self.assertIsNotNone(ranked)
        self.assertGreater(ranked[0], 0.0)


if __name__ == "__main__":
    unittest.main()
