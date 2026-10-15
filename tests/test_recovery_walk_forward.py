"""Probability and chronological-calibration contracts for the recovery runner."""
import importlib.util
from pathlib import Path
import unittest

P = Path(__file__).resolve().parents[1] / 'scripts/betting_v2/recovery_walk_forward.py'
spec = importlib.util.spec_from_file_location('recovery', P)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class RecoveryContracts(unittest.TestCase):
    def test_coherent_goal_probabilities(self):
        row = dict(metric='GOALS_FT', means=(2.1, .8), actual=(1, 1))
        for alpha in [0., .3]:
            raw = r.events(row,1.1,alpha)
            events = {name:(p,y) for name,p,y in raw}
            self.assertEqual(len(raw),len(events))
            home, draw, away = [events['1X2_'+v][0] for v in ['HOME','DRAW','AWAY']]
            self.assertAlmostEqual(home+draw+away,1.)
            self.assertAlmostEqual(events['DC_1X'][0],home+draw)
            self.assertAlmostEqual(events['DC_X2'][0],away+draw)
            self.assertEqual(events['BTTS_YES'][1],1)
            self.assertGreater(events['MATCH_OVER_1.5'][0],events['MATCH_OVER_2.5'][0])

    def test_no_calibration_fit_on_tiny_population(self):
        self.assertFalse(r.fit_calibration([dict(means=(1.,1.),actual=(1,0))])['fitted'])

    def test_dispersion_is_fitted_on_provided_prior_rows_only(self):
        rows = [dict(means=(2.,2.), actual=(0,4)) for _ in range(120)]
        fit=r.fit_calibration(rows)
        self.assertTrue(fit['fitted'])
        self.assertGreater(fit['alpha'],.1)
        self.assertAlmostEqual(sum(r.distribution(2.,fit['scale'],fit['alpha'])),1.)


if __name__=='__main__':
    unittest.main()
