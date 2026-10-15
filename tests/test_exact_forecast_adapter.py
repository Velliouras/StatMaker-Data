import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.validated_walk_forward import distribution


class AdapterTest(unittest.TestCase):
    def test_scoring_and_explanation_use_the_same_distribution(self):
        row=dict(metric='GOALS_FT',regime='XG',means=[2.,1.])
        profile={'GOALS_FT::XG':dict(scale=1.2,alpha=.1,draw_tilt=.2,btts_tilt=-.1)}
        spec=('GOALS_FT','DNB','HOME','',None)
        result=forecast(row,profile,spec);joint=distribution(row,profile)
        self.assertEqual((result['winProbability'],result['pushProbability']),joint.evaluate(spec))
        self.assertEqual((result['homeMean'],result['awayMean']),joint.means)
        self.assertAlmostEqual(sum(result[k] for k in ('winProbability','pushProbability','lossProbability')),1.)
        self.assertEqual(result['actualRuns'],0)
        self.assertIsNone(result['validatedTier'])

    def test_half_scenario_conditions_on_fulltime_winner(self):
        row=dict(metric='GOALS_1H',regime='XG',means=[.8,.4],fullMeans=[2.,1.],shares=[.4,.4])
        result=forecast(row,{},('GOALS_1H','RESULT','AWAY','',None))
        full=distribution(dict(metric='GOALS_FT',regime='XG',means=[2.,1.]),{})
        conditional,mass=full.condition('HOME')
        self.assertAlmostEqual(result['scenarios']['HOME']['conditionMass'],mass)
        self.assertAlmostEqual(result['scenarios']['HOME']['winProbability'],
            conditional.thin(.4,.4).evaluate(('GOALS_1H','RESULT','AWAY','',None))[0])

    def test_metric_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            forecast(dict(metric='CORNERS'),{},('GOALS_FT','RESULT','HOME','',None))
