import sys, unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.probability_recalibration import fit, bank, canonical_event, probability, decision
from betting_v2.research_probability_recalibration import contract, price_attribution


class RecalibrationTests(unittest.TestCase):
    def records(self):
        return [dict(metric='CORNERS', scope='TOTAL', fixture=str(i), kickoff=100000.+i,
            pOver=.7, yOver=float(i%2)) for i in range(40)]

    def test_full_day_label_lag_and_future_immunity(self):
        rows=self.records(); cutoff=300000.
        expected=bank(rows, 'CORNERS', 'TOTAL', cutoff)
        future=dict(rows[0], fixture='future', kickoff=cutoff-1., pOver=.99, yOver=1.)
        self.assertEqual(bank(rows+[future], 'CORNERS', 'TOTAL', cutoff), expected)
        with self.assertRaises(ValueError): fit(rows+[future], cutoff)
        boundary=dict(rows[0], kickoff=cutoff-86400.)
        fit([boundary], cutoff)

    def test_opposite_sides_are_one_canonical_event(self):
        over=('CORNERS','TOTAL','OVER','',10.)
        under=('CORNERS','TOTAL','UNDER','',10.)
        a=canonical_event(over,.6,.1,1.)
        b=canonical_event(under,.3,.1,0.)
        np.testing.assert_allclose(a,b,atol=1e-12)

    def test_push_mass_and_partition_complements_preserved(self):
        over=('CORNERS','TOTAL','OVER','',10.)
        under=('CORNERS','TOTAL','UNDER','',10.)
        for theta in ([0.,1.],[-.4,.7],[.8,1.3]):
            p=probability(over,.6,.1,theta); q=probability(under,.3,.1,theta)
            self.assertAlmostEqual(p+q+.1,1.)
        self.assertAlmostEqual(probability(over,.6,.1,[0.,1.]),.6)

    def test_monotone_fit_reduces_known_overconfidence_without_roi(self):
        fit_result=fit(self.records(),300000.)
        self.assertTrue(fit_result['fitted']); self.assertFalse(fit_result['roiUsed'])
        theta=fit_result['theta']; spec=('CORNERS','TOTAL','OVER','',9.5)
        p=probability(spec,.7,0.,theta)
        self.assertLess(abs(p-.5),.2)
        values=[probability(spec,x,0.,theta) for x in (.1,.3,.5,.7,.9)]
        self.assertEqual(values,sorted(values))

    def test_fixture_total_weight_does_not_grow_with_more_quotes(self):
        rows=self.records(); expected=fit(rows,300000.)['theta']
        duplicated=rows+[dict(rows[0]) for _ in range(25)]
        np.testing.assert_allclose(fit(duplicated,300000.)['theta'],expected,atol=1e-10)

    def test_insufficient_prior_fixtures_is_inactive(self):
        result=fit(self.records()[:29],300000.)
        self.assertFalse(result['fitted']);self.assertEqual(result['theta'],[0.,1.])

    def test_decisions_never_raise_raw_probability_or_relax_thresholds(self):
        pick=dict(p=.7,push=.1,odd=2.,confidence=.65,preference=0,
            spec=['CORNERS','TOTAL','OVER','',10.])
        self.assertEqual(decision(pick,.9)['p'],.7)
        self.assertIsNone(decision(pick,.59999))
        for odd in (1.8,3.): self.assertIsNone(decision(dict(pick,odd=odd),.7))
        result=decision(pick,.6)
        self.assertEqual(result['preference'],0);self.assertEqual(result['push'],.1)
        self.assertAlmostEqual(result['ev'],.3)

    def test_price_attribution_requires_exact_contract_book_and_event(self):
        q=dict(fixture='f',providerEventId='10',bookmaker='A',spec=['SOT','TEAM','OVER','HOME',4.5],odd=2.,observed=1.)
        pick=dict(q,y=1.,actualPush=0.)
        last=dict(q,odd=2.2,observed=2.)
        r=price_attribution([q],[last,dict(last,bookmaker='B')],[pick])
        self.assertEqual(r['matchedFixedEarliestPicks'],1)
        self.assertAlmostEqual(r['lastProfitUnits']-r['firstProfitUnits'],.2)
        self.assertNotEqual(contract(q),contract(dict(q,providerEventId='11')))
        self.assertEqual(price_attribution([q],[dict(last,providerEventId='11')],[pick])['matchedFixedEarliestPicks'],0)
        self.assertEqual(price_attribution([q],[last,dict(last,odd=2.3)],[pick])['matchedFixedEarliestPicks'],0)


if __name__=='__main__': unittest.main()
