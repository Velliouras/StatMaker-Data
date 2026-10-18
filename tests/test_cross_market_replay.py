import math,sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.research_cross_market import probability_scores,prepared_law,law_value,KINDS
from betting_v2.paired_count import from_means

class CrossReplayTests(unittest.TestCase):
    def test_forecast_adapter_preserves_both_sides_and_conditionals(self):
        prepared=prepared_law(from_means(6.,4.,alpha=.1,concentration=30.))
        for scope,team,line in [('TOTAL','',10.),('TEAM','HOME',5.5),('TEAM','AWAY',4.)]:
            a=law_value(prepared,('CORNERS',scope,'OVER',team,line));b=law_value(prepared,('CORNERS',scope,'UNDER',team,line))
            self.assertAlmostEqual(a['winProbability']+b['winProbability']+a['pushProbability'],1.,places=10)
            self.assertAlmostEqual(a['pushProbability'],b['pushProbability'])
            self.assertGreaterEqual(a['scenarios']['HOME']['conditionMass'],0.)

    def test_push_uses_three_state_probability_and_not_a_loss(self):
        probs={k:dict(win=.4,push=.2) for k in KINDS}
        result=probability_scores([dict(fixture='f',actualWin=0.,actualPush=1.,probabilities=probs)])
        for kind in KINDS:
            self.assertAlmostEqual(result[kind]['threeStateLogLoss'],-math.log(.2))
            self.assertIsNone(result[kind]['conditionalLogLoss'])

    def test_more_lines_do_not_increase_fixture_weight(self):
        a={k:dict(win=.8,push=0.) for k in KINDS};b={k:dict(win=.3,push=0.) for k in KINDS}
        rows=[dict(fixture='a',actualWin=1.,actualPush=0.,probabilities=a),dict(fixture='b',actualWin=1.,actualPush=0.,probabilities=b)]
        first=probability_scores(rows);other=probability_scores(rows+[rows[0]]*10)
        for kind in KINDS:self.assertAlmostEqual(first[kind]['threeStateLogLoss'],other[kind]['threeStateLogLoss'])

    def test_invalid_probability_mass_is_rejected(self):
        p={k:dict(win=.9,push=.2) for k in KINDS}
        with self.assertRaises(ValueError):probability_scores([dict(fixture='f',actualWin=1.,actualPush=0.,probabilities=p)])

if __name__=='__main__':unittest.main()
