"""Artifact checks run after all-market training, before UAT publication."""
import gzip
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from betting_v2.live_revised import load_revised
from betting_v2.reliable_counts import METRICS
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.opponent_matchup import timestamp


class AllMarketDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model,cls.profile,cls.meta=load_revised(ROOT,[],timestamp('2026-10-01'))
        cls.directory=ROOT/'reports/betting_v2/all_market_revision_20261016'

    def test_all_seven_metric_states_are_active_and_hash_verified(self):
        self.assertEqual(set(self.model.counts.models),set(METRICS))
        self.assertEqual(set(self.meta['revisedCountMetrics']),set(METRICS))
        self.assertEqual(self.meta['otherCountWeights'],'retrained per metric')
        self.assertFalse(self.meta['profitabilityValidated'])
        for metric,m in self.model.counts.models.items():
            self.assertEqual(m.metric,metric);self.assertEqual(len(m.fits),13)
            self.assertEqual(m.clock,timestamp('2026-10-01'))
            self.assertLess(m.anchor['maxTrainingTimestamp'],m.trainingBefore)

    def test_first_day_research_forecasts_are_reproduced_without_live_fitting(self):
        with patch('betting_v2.reliable_goals.weighted_fit',side_effect=AssertionError('Live refit')):
            for metric in METRICS:
                p=self.directory/(metric+'_2026-10-01_predictions.json.gz')
                rows=json.loads(gzip.decompress(p.read_bytes()))
                day=[r for r in rows if r['featureCutoff']==timestamp('2026-10-01')]
                self.assertTrue(day,'No October-first-day artifact '+metric)
                for row in day[:3]:
                    payload=row['fixture'].split(':',1)[1]
                    pairs=[(h,payload[len(h)+1:]) for h in self.model.history
                        if payload.startswith(h+':') and payload[len(h)+1:] in self.model.history]
                    self.assertEqual(len(pairs),1)
                    pair=pairs[0];pred=self.model.predict(*pair,metric)
                    np.testing.assert_allclose(pred[:2],row['means'],rtol=1e-12,atol=1e-12)
                    spec=(metric,'TOTAL','OVER','',.5 if metric=='RED_CARDS' else 4.5)
                    target,p,prepared=self.model.prepared_bank(*pair,metric)[0]
                    point=forecast(target,self.profile,spec);bankpoint=forecast(target,p,spec,prepared)
                    self.assertAlmostEqual(point['winProbability'],bankpoint['winProbability'])
                    bound=self.model.bounds(*pair,spec,2.2)
                    self.assertLessEqual(bound['win'],point['winProbability'])
                    self.assertLessEqual(bound['push'],point['pushProbability'])
                    self.assertAlmostEqual(bound['ev'],bound['win']*2.2+bound['push']-1)

if __name__=='__main__':unittest.main()
