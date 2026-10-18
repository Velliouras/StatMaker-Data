import sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.context_market import FEATURES,feature_vector,fit,bank,predict
from betting_v2.research_context_market import partitions,eligible,conservative_winner,prematch_context
from betting_v2.research_scope_expansion import select_scope


class ContextMarketTests(unittest.TestCase):
    def records(self):
        rows=[]
        for i in range(120):
            x=[0.]*len(FEATURES);x[0]=1.;x[FEATURES.index('elo_context')]=1. if i%2 else -1.
            rows.append(dict(fixture=str(i),kickoff=100000.+i,featureCutoff=1000.,x=x,marketOver=.5,yOver=float(i%2)))
        return rows

    def test_zero_parameters_are_exactly_the_market_and_complements_normalize(self):
        parameters=dict(featureIndexes=list(range(len(FEATURES))),coef=[0.]*len(FEATURES))
        q=predict([0.]*len(FEATURES),.43,parameters)
        self.assertAlmostEqual(q,.43);self.assertAlmostEqual(q*.9+(1-q)*.9+.1,1.)

    def test_context_learns_signal_that_simple_baseline_cannot(self):
        rows=self.records();context=fit(rows,300000.,'context');simple=fit(rows,300000.,'simple')
        self.assertTrue(context['fitted']);self.assertFalse(context['roiUsed'])
        self.assertGreater(predict(rows[1]['x'],.5,context),predict(rows[0]['x'],.5,context))
        self.assertLess(context['loss'],simple['loss'])

    def test_current_and_future_labels_cannot_affect_prior_bank(self):
        rows=self.records();cutoff=300000.;expected=bank(rows,cutoff,'context')
        future=dict(rows[0],fixture='future',kickoff=cutoff-1.,yOver=1.)
        self.assertEqual(bank(rows+[future],cutoff,'context'),expected)
        with self.assertRaises(ValueError):fit(rows+[future],cutoff,'context')

    def test_minimum_prior_sample_does_not_silently_fit(self):
        self.assertFalse(fit(self.records()[:99],300000.)['fitted'])

    def test_duplicates_do_not_change_fixture_total_weight(self):
        rows=self.records();a=fit(rows,300000.);b=fit(rows+[dict(rows[0]) for _ in range(20)],300000.)
        np.testing.assert_allclose(a['coef'],b['coef'],atol=1e-8)

    def q(self,side='OVER',book='Bet365'):
        return dict(fixture='f',sourceReceipt='r',providerEventId='1',bookmaker=book,observed=200000.,kickoff=300000.,
            featureCutoff=100000.,odd=2.,spec=['CORNERS','TOTAL',side,'',9.5])

    def test_only_complete_same_clock_partitions_and_real_other_books(self):
        rows=[self.q(),self.q('UNDER'),self.q(book='Unibet'),self.q('UNDER','Unibet')]
        groups,_=partitions(rows)
        self.assertEqual(len(groups),2);self.assertTrue(all(g['reference']['independent'] for g in groups))
        self.assertEqual(partitions([self.q()])[0],[])
        bad=dict(self.q('UNDER'),observed=200001.)
        with self.assertRaises(ValueError):partitions([self.q(),bad])
        conflict=dict(self.q(),odd=2.2)
        groups,_=partitions(rows+[conflict]);self.assertEqual(len(groups),1)
        self.assertFalse(groups[0]['reference']['independent'])

    def test_features_ignore_labels_and_missing_xg_is_explicit(self):
        q=self.q();c=dict(sample_home=10,sample_away=8,xg_home=0,xg_away=0,count_recent=10.,count_venue=9.,
            elo=.5,xg_level=1.5,goal_form=1.4,market_updated=190000.,margin=.05,independent=True)
        a=feature_vector(q,.6,.5,10.,c);b=feature_vector(dict(q,y=0.,final_count=99),.6,.5,10.,c)
        self.assertEqual(a,b);self.assertEqual(a[FEATURES.index('xg_quality')],0.)
        self.assertEqual(a[FEATURES.index('xg_missing')],1.)
        self.assertTrue(np.isfinite(a).all())

    def test_original_odds_probability_and_favourite_rules_remain(self):
        class Full:
            def evaluate(self,spec):return ((.7 if spec[2]=='HOME' else .2),0.)
        v=dict(homeMean=6.,awayMean=4.,scenarios={'HOME':dict(winProbability=.8)})
        self.assertIsNone(eligible(self.q(),v,Full(),'HOME',.59,0.))
        for odd in (1.8,3.):self.assertIsNone(eligible(dict(self.q(),odd=odd),v,Full(),'HOME',.7,0.))
        self.assertIsNotNone(eligible(self.q(),v,Full(),'HOME',.7,0.))
        self.assertIsNone(eligible(self.q(),dict(v,scenarios={'HOME':dict(winProbability=.59)}),Full(),'HOME',.7,0.))

    def test_conservative_rejection_never_replaces_the_point_winner(self):
        first=dict(self.q(),p=.7,push=0.,ev=.4,confidence=.7,preference=0,_lower=dict(p=.55,push=0.,support=.7))
        runner=dict(self.q(),p=.8,push=0.,ev=.6,confidence=.8,preference=1,_lower=dict(p=.7,push=0.,support=.7))
        frozen=select_scope([first,runner]);self.assertEqual(frozen,[first])
        self.assertIsNone(conservative_winner(frozen[0]));self.assertIsNotNone(conservative_winner(runner))

    def test_real_prematch_features_exclude_future_history_changes(self):
        from betting_v2.research_probability_recalibration import MonthlyModels,ROOT,PRIOR,OCTOBER,read
        from betting_v2.context_market import METRICS
        history=read(PRIOR/'history.json.gz')
        chosen=read(OCTOBER/'priced_latest.json')['reports']['coupled_conservative']['picks'][0]
        cutoff=chosen['featureCutoff'];day='2026-10-01'
        profiles={day:{m:read(ROOT/f'reports/betting_v2/combined_candidate_20261016/{m}_profiles.json') for m in METRICS}}
        first=MonthlyModels(history,profiles);first.advance(cutoff)
        future=dict(history[-1],_date=cutoff+86400,normalized_stats={'HS':99,'AS':99,'HxG':99,'AxG':99})
        second=MonthlyModels(history+[future],profiles);second.advance(cutoff)
        a=prematch_context(first,chosen,.05,True);b=prematch_context(second,chosen,.05,True)
        self.assertEqual(a,b)
        with self.assertRaises(ValueError):prematch_context(first,dict(chosen,featureCutoff=cutoff-86400),.05,True)


if __name__=='__main__':unittest.main()
