import gzip,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import materialize_betting_v2_direct as direct
from betting_v2.live_exact import restore,load_live
from betting_v2.opponent_matchup import timestamp


class FrozenLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history=direct.load_actual_results(timestamp('2026-10-15'))['GLOBAL']

    def test_reloaded_weights_reproduce_saved_backtest_predictions(self):
        folder=ROOT/'reports/betting_v2/corrected_20261015'
        state=json.loads(gzip.decompress((folder/'full_2026-10-01_pretest_state.json.gz').read_bytes()))
        with patch('betting_v2.monotone_count.fit_monotone_count',side_effect=AssertionError('Refit forbidden')):
            model=restore(state,self.history,timestamp('2026-09-01'),direct.metric_values)
        rows=json.loads((ROOT/'tests/fixtures/exact_frozen_october.json').read_text())
        for r in rows:
            # Scoped identities themselves include colons.
            pair=next((v['_home'],v['_away']) for v in self.history
                if f"{v['_date']}:{v['_home']}:{v['_away']}"==r['fixture'])
            pred=model.predict(*pair,r['metric'])
            np.testing.assert_allclose(pred[:2],r['means'],rtol=1e-12,atol=1e-12)
            self.assertEqual(list(pred[2:4]),r['samples'])

    def test_current_day_cannot_enter_live_features(self):
        cutoff=timestamp('2026-10-15')
        calls=[]
        from betting_v2.opponent_matchup import OpponentMatchup
        original=OpponentMatchup.observe
        def observed(model,row,values):
            calls.append(row['_date']);return original(model,row,values)
        with patch.object(OpponentMatchup,'observe',observed):
            _,_,meta=load_live(ROOT,self.history,cutoff)
        self.assertTrue(calls)
        self.assertTrue(all(timestamp('2026-10-01')<=v<cutoff for v in calls))
        self.assertEqual(meta['model'],'betting-v2-joint-count-v4')
        self.assertEqual(meta['calibrationBefore'],'2026-10-01')

    def test_changed_training_source_is_rejected(self):
        changed=[dict(r) for r in self.history]
        changed[0]['home_goals']+=1
        with self.assertRaisesRegex(ValueError,'training history changed'):
            load_live(ROOT,changed,timestamp('2026-10-15'))
