import sys,unittest
from pathlib import Path
import numpy as np
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'),str(Path(__file__).resolve().parents[1]/'scripts/betting_v2')]
from betting_v2.research_prior_count_prices import (
    absolute_time,fold_before,prior_records,price_bank,capped_probability,cap_pick,
    lower_pick,FrozenMonthlyModels,COUNT_METRICS,ROOT,read)


class PriorCountPriceTests(unittest.TestCase):
    def test_monthly_model_never_comes_from_after_feature_cutoff(self):
        with self.assertRaises(ValueError):fold_before(absolute_time('2026-07-31T23:59:59Z'))
        self.assertEqual(fold_before(absolute_time('2026-08-31T23:59:59Z')),'2026-08-01')
        self.assertEqual(fold_before(absolute_time('2026-09-01T00:00:00Z')),'2026-09-01')

    def test_price_calibration_requires_a_full_day_of_label_lag(self):
        rows=[dict(metric='CORNERS',kickoff=10.),dict(metric='CORNERS',kickoff=11.),dict(metric='SOT',kickoff=1.)]
        self.assertEqual(prior_records(rows,'CORNERS',86410.),[rows[0]])

    def test_future_labels_cannot_change_prior_price_weights(self):
        cutoff=absolute_time('2026-09-01T00:00:00Z')
        rows=[dict(metric='CORNERS',fixture=str(i),kickoff=cutoff-86400*(2+i%5),
            statConditional=.7,marketConditional=.5,y=float(i%3==0)) for i in range(35)]
        expected=price_bank(rows,'CORNERS',cutoff)
        future=dict(metric='CORNERS',fixture='future',kickoff=cutoff-100.,statConditional=.99,marketConditional=.01,y=1.)
        self.assertEqual(price_bank(rows+[future],'CORNERS',cutoff),expected)

    def test_probability_cap_preserves_push_accounting_and_never_increases_win(self):
        for weight in (0.,.5,1.):
            capped=capped_probability(.6,.1,.5,weight)
            self.assertLessEqual(capped,.6);self.assertGreaterEqual(capped,0.)
        self.assertAlmostEqual(capped_probability(.6,.1,.5,0.),.45)

    def test_cap_keeps_original_selection_requirements(self):
        pick=dict(p=.7,push=0.,odd=2.,confidence=.7,spec=['CORNERS','TOTAL','OVER','',9.5])
        self.assertIsNone(cap_pick(pick,.59))
        result=cap_pick(pick,.6)
        self.assertEqual(result['spec'],pick['spec']);self.assertEqual(result['odd'],pick['odd'])
        self.assertAlmostEqual(result['ev'],.2)

    def test_conservative_pick_uses_replica_sensitivity_and_favourite_support(self):
        def value(p,support):return dict(winProbability=p,pushProbability=0.,scenarios={'HOME':dict(winProbability=support)})
        pick=dict(odd=2.,p=.8,push=0.,confidence=.8,spec=['CORNERS','TOTAL','OVER','',9.5])
        values=[value(.8,.8)]+[value(.65,.7) for _ in range(12)]
        self.assertAlmostEqual(lower_pick(pick,values,'HOME')['p'],.65)
        self.assertIsNone(lower_pick(pick,[value(.8,.8)]+[value(.65,.59) for _ in range(12)],'HOME'))

    def test_incremental_advance_matches_one_jump_and_excludes_future_rows(self):
        history=read(ROOT/'reports/betting_v2/git_archive_expansion_20261016/history.json.gz')
        source=ROOT/'reports/betting_v2/all_market_revision_20261016'
        laws={'2026-08-01':{m:read(source/f'{m}_2026-08-01_profiles.json') for m in COUNT_METRICS}}
        cutoff=absolute_time('2026-08-06T00:00:00Z')
        fixture=next(r for r in history if cutoff<=r['_date']<cutoff+86400 and r.get('_home') and r.get('_away'))
        first=FrozenMonthlyModels(history,laws);first.advance(cutoff)
        second=FrozenMonthlyModels(history,laws);second.advance(cutoff-86400);second.advance(cutoff)
        np.testing.assert_allclose(first.ft.predict_bank(fixture['_home'],fixture['_away']),
            second.ft.predict_bank(fixture['_home'],fixture['_away']),rtol=0,atol=0)
        changed=dict(fixture,_date=cutoff+86400,home_goals=50,away_goals=50)
        third=FrozenMonthlyModels(history+[changed],laws);third.advance(cutoff)
        np.testing.assert_allclose(first.ft.predict_bank(fixture['_home'],fixture['_away']),
            third.ft.predict_bank(fixture['_home'],fixture['_away']),rtol=0,atol=0)
        for metric in COUNT_METRICS:
            reference=first.counts[metric].predict_bank(fixture['_home'],fixture['_away'])
            for other in (second,third):
                result=other.counts[metric].predict_bank(fixture['_home'],fixture['_away'])
                if reference is None:self.assertIsNone(result)
                else:np.testing.assert_allclose(reference,result,rtol=0,atol=0)
        with self.assertRaises(ValueError):second.advance(cutoff-1)


if __name__=='__main__':unittest.main()
