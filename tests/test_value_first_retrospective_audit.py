import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import value_first_retrospective_audit as audit

class ValueFirstRetrospectiveMathTest(unittest.TestCase):
    def test_roi_and_unresolved_are_separate(self):
        rows=[dict(date='2026-10-01',matchKey='a',selectionKey='x',odd=1.8,outcome='WON',grossReturn=1.8),
              dict(date='2026-10-01',matchKey='b',selectionKey='y',odd=1.8,outcome='LOST',grossReturn=0),
              dict(date='2026-10-01',matchKey='c',selectionKey='z',odd=1.8,outcome='VOID',grossReturn=1),
              dict(date='2026-10-01',matchKey='d',selectionKey='t',odd=2.0,outcome=None,grossReturn=None)]
        x=audit.summarize(rows)
        self.assertEqual((x['picks'],x['graded'],x['unresolved'],x['won'],x['lost'],x['void']),(4,3,1,1,1,1))
        self.assertAlmostEqual(x['roi'],-0.2/3)
        self.assertAlmostEqual(x['maxDrawdownUnits'],1.0)
    def test_odds_buckets(self):
        self.assertEqual(audit.odds_band(1.8),'1.80–1.99')
        self.assertEqual(audit.odds_band(2.5),'2.50+')
