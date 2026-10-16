import copy
import unittest
import numpy as np
from betting_v2.research_revision_calibration import freeze_pool
from betting_v2.selected_joint_calibration import cell_mass
from betting_v2.joint_count import JointCount
from betting_v2.opponent_matchup import timestamp


class ExpandingCalibrationTests(unittest.TestCase):
    def pool(self):
        base=cell_mass(JointCount.from_means(2.3,.7)).tolist()
        return [dict(fixture=str(i),date=timestamp('2026-08-01')+i*86400,
            spec=('GOALS_FT','RESULT','HOME','',None),y=int(i%3==0),base=base,
            bootstrapBases=[base]*12,sourceFrame='August out-of-time') for i in range(30)]

    def test_later_month_cannot_enter_earlier_fit(self):
        with self.assertRaisesRegex(ValueError,'future'):
            freeze_pool(self.pool(),timestamp('2026-08-15'),1)

    def test_duplicate_events_cannot_inflate_calibration_sample(self):
        rows=self.pool()
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            freeze_pool(rows+[copy.deepcopy(rows[0])],timestamp('2026-10-01'),1)

    def test_prior_frame_sources_and_replica_count_are_preserved(self):
        bank=freeze_pool(self.pool(),timestamp('2026-09-01'),7)
        self.assertEqual(bank['point']['fixtures'],30);self.assertTrue(bank['point']['fitted'])
        self.assertEqual(len(bank['replicas']),12);self.assertEqual(bank['sourceFrames'],['August out-of-time'])
        self.assertLess(bank['trainingBefore'],timestamp('2026-09-01'))


if __name__=='__main__':unittest.main()
