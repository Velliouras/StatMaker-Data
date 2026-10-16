import copy
import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.reliable_counts import ReliableCounts,metric_values,rolling_predictions,METRICS
from betting_v2.chronological_stack import PriorGoalBank
from test_model_revision import history
import materialize_betting_v2_direct as direct


def counts_history():
    rows=history()
    for i,r in enumerate(rows):
        r['normalized_stats'].update(HC=3+i%5,AC=2+i%3,HS=10+i%7,AS=8+i%5,HST=3+i%3,AST=2+i%2,
            HY=i%4,AY=i%3,HR=int(i%13==0),AR=int(i%17==0),HF=8+i%8,AF=10+i%6)
    return rows


class ReliableCountTests(unittest.TestCase):
    def test_all_families_have_distinct_targets_and_roundtrip_sensitivity_banks(self):
        rows=counts_history();cutoff=direct.timestamp('2026-02-01');bank=PriorGoalBank(rows,metric_values)
        for metric in METRICS:
            with self.subTest(metric=metric):
                m=ReliableCounts(dict(metric=metric,bootstrapReplicas=2))
                design=m.design(rows,cutoff,metric_values,bank=bank)
                expected=[v for r in rows if r['_date']<cutoff for v in metric_values(r,metric)]
                np.testing.assert_array_equal(design[1],expected)
                m.fit_bank(design,seed=7)
                restored=ReliableCounts.restore(m.export())
                np.testing.assert_allclose(m.predict_bank('a','b'),restored.predict_bank('a','b'),rtol=0,atol=0)
                self.assertEqual(m.predict_bank('a','b').shape,(3,2))
                self.assertIsNone(m.predict('a','b','GOALS_FT'))

    def test_future_labels_cannot_change_count_features_or_goal_stacking(self):
        rows=counts_history();cutoff=direct.timestamp('2026-02-01')
        altered=copy.deepcopy(rows)
        for r in altered:
            if r['_date']>=cutoff:r['normalized_stats'].update(HC=999,AC=999);r['home_goals']=99
        designs=[]
        for source in (rows,altered):
            m=ReliableCounts(dict(metric='CORNERS',bootstrapReplicas=0))
            designs.append(m.design(source,cutoff,metric_values,bank=PriorGoalBank(source,metric_values)))
        self.assertEqual((designs[0][0]!=designs[1][0]).nnz,0)
        np.testing.assert_array_equal(designs[0][1],designs[1][1])

    def test_same_day_counts_are_unseen_and_snapshot_is_immutable(self):
        rows=counts_history();cutoff=direct.timestamp('2026-02-01')
        m=ReliableCounts(dict(metric='CARDS',bootstrapReplicas=0))
        d=m.design(rows,cutoff,metric_values,bank=PriorGoalBank(rows,metric_values));m.fit_bank(d,bootstrap=False)
        state=m.export();frozen=copy.deepcopy(state)
        first=rolling_predictions(m,rows,cutoff,cutoff+86400)
        altered=copy.deepcopy(rows)
        for r in altered:
            if cutoff<=r['_date']<cutoff+86400:r['normalized_stats'].update(HY=9,AY=9,HR=3,AR=3)
        second=rolling_predictions(ReliableCounts.restore(state),altered,cutoff,cutoff+86400)
        self.assertEqual([r['means'] for r in first],[r['means'] for r in second]);self.assertEqual(state,frozen)

    def test_red_cards_require_actual_separate_labels(self):
        row=dict(normalized_stats=dict(HY=4,AY=3))
        self.assertIsNone(metric_values(row,'RED_CARDS'))
        row['normalized_stats'].update(HR=1,AR=0)
        self.assertEqual(metric_values(row,'RED_CARDS'),(1.,0.))
        self.assertEqual(metric_values(row,'CARDS'),(5.,3.))

if __name__=='__main__':unittest.main()
