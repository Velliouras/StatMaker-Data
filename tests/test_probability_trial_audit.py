import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.audit_probability_trial import canonical_summary


class ProbabilityAuditTests(unittest.TestCase):
    def test_seeded_fixture_interval_is_independent_of_input_order(self):
        rows=[dict(fixture=str(i//2),spec=['SOT','TEAM','OVER','HOME' if i%2 else 'AWAY',4.5],bookmaker='A',
            p=.65,push=0.,odd=2.,y=float(i%3==0),actualPush=0.) for i in range(80)]
        self.assertEqual(canonical_summary(rows),canonical_summary(list(reversed(rows))))

    def test_empty_summary_remains_unscored(self):
        self.assertEqual(canonical_summary([]),dict(bets=0,fixtures=0))


if __name__=='__main__':unittest.main()
