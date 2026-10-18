import sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.winner_calibration import fit,bank,adjusted,decide
from betting_v2.research_scope_expansion import select_scope


class WinnerCalibrationTests(unittest.TestCase):
    def rows(self):
        return [dict(fixture=str(i),kickoff=100000.+i,p=.7,push=0.,y=float(i%2),actualPush=0.) for i in range(40)]

    def test_current_and_future_labels_cannot_change_prior_fit(self):
        rows=self.rows();cutoff=300000.;expected=bank(rows,cutoff)
        future=dict(rows[0],fixture='future',kickoff=cutoff-1.,y=1.)
        self.assertEqual(bank(rows+[future],cutoff),expected)
        with self.assertRaises(ValueError):fit(rows+[future],cutoff)

    def test_pushes_excluded_and_return_mass_preserved(self):
        rows=self.rows();before=bank(rows,300000.)
        self.assertEqual(bank(rows+[dict(rows[0],fixture='push',actualPush=1.)],300000.),before)
        self.assertAlmostEqual(adjusted(.6,.1,0.),.6)
        self.assertLessEqual(adjusted(.6,.1,2.),.9)

    def test_one_parameter_corrects_known_overconfidence(self):
        f=fit(self.rows(),300000.)
        self.assertTrue(f['fitted']);self.assertFalse(f['roiUsed'])
        self.assertLess(f['bias'],0.)
        self.assertLess(abs(adjusted(.7,0.,f['bias'])-.5),.2)

    def test_more_quotes_of_one_fixture_do_not_increase_its_weight(self):
        rows=self.rows();expected=fit(rows,300000.)['bias']
        self.assertAlmostEqual(fit(rows+[dict(rows[0]) for _ in range(20)],300000.)['bias'],expected)

    def test_insufficient_prior_winners_abstain(self):
        parameters=bank(self.rows()[:29],300000.)
        self.assertFalse(parameters['point']['fitted'])
        self.assertIsNone(decide(dict(p=.8,push=0.,odd=2.,confidence=.8),parameters))

    def parameters(self,bias):
        return dict(point=dict(fitted=True,bias=bias),replicas=[dict(bias=bias) for _ in range(12)])

    def test_rejected_winner_never_falls_back_to_another_line(self):
        common=dict(fixture='f',bookmaker='A',push=0.,odd=2.,ev=.3,tier='DEVELOPING')
        first=dict(common,spec=['CORNERS','TEAM','OVER','HOME',5.5],p=.61,confidence=.61,preference=0)
        runner=dict(common,spec=['CORNERS','TOTAL','OVER','',8.5],p=.8,confidence=.8,preference=1)
        parameters=self.parameters(-.4)
        frozen=select_scope([first,runner])
        self.assertEqual(frozen,[first])
        self.assertIsNone(decide(frozen[0],parameters))
        self.assertIsNotNone(decide(runner,parameters))

    def test_thresholds_labels_and_original_score_are_respected(self):
        winner=dict(p=.7,push=.1,odd=2.,confidence=.65,y=1.,actualPush=0.)
        result=decide(winner,self.parameters(1.))
        self.assertEqual(result['p'],winner['p']);self.assertNotIn('y',result)
        self.assertEqual(result,decide(dict(winner,y=0.),self.parameters(1.)))
        self.assertIsNone(decide(dict(winner,odd=1.8),self.parameters(0.)))
        self.assertIsNone(decide(dict(winner,odd=3.),self.parameters(0.)))
        self.assertIsNone(decide(dict(winner,p=.59),self.parameters(1.)))

    def test_weekly_sensitivity_can_only_reduce_conservative_decision(self):
        winner=dict(p=.8,push=0.,odd=2.,confidence=.8)
        parameters=dict(point=dict(fitted=True,bias=-.1),replicas=[dict(bias=-.5) for _ in range(12)])
        self.assertLess(decide(winner,parameters,True)['p'],decide(winner,parameters,False)['p'])


if __name__=='__main__':unittest.main()
