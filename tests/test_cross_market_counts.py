import copy,math,sys,unittest
from pathlib import Path
import numpy as np
from scipy.special import expit
from scipy.stats import poisson,skellam
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.cross_market_inputs import anchors,goal_rates,valid_quote
from betting_v2.cross_market_counts import FEATURES,PENALTY,design,objective,fit,bank,distribution,feature_vector,parameters,joint_log_probability

class CrossMarketInputTests(unittest.TestCase):
    def quotes(self,book='Bet365'):
        b=dict(fixture='f',providerEventId='12',sourceReceipt='r',observed=300000.,kickoff=400000.,featureCutoff=172800.,marketUpdatedAtUTC=290000.,bookmaker=book)
        r={'HOME':float(skellam.sf(0,1.7,1.1)),'DRAW':float(skellam.pmf(0,1.7,1.1)),'AWAY':float(skellam.cdf(-1,1.7,1.1))}
        q=[dict(b,spec=['GOALS_FT','RESULT',s,'',None],odd=1/(p*1.05)) for s,p in r.items()];o=float(poisson.sf(2,2.8))
        return q+[dict(b,spec=['GOALS_FT','TOTAL',s,'',2.5],odd=1/(p*1.05)) for s,p in [('OVER',o),('UNDER',1-o)]]

    def test_inversion_recovers_paired_means_for_integer_and_half_lines(self):
        r={'HOME':float(skellam.sf(0,1.7,1.1)),'DRAW':float(skellam.pmf(0,1.7,1.1)),'AWAY':float(skellam.cdf(-1,1.7,1.1))}
        for line in (2.,2.5,3.):
            push=float(poisson.pmf(int(line),2.8)) if line.is_integer() else 0.;p=goal_rates(r,float(poisson.sf(math.floor(line),2.8)/(1-push)),line)
            self.assertAlmostEqual(p['home'],1.7,places=8);self.assertAlmostEqual(p['away'],1.1,places=8);self.assertAlmostEqual(p['drawResidual'],0.,places=8)

    def test_unsupported_lines_and_incomplete_result_are_rejected(self):
        for r,line in [({'HOME':.5,'DRAW':.25,'AWAY':.25},2.25),({'HOME':.5,'AWAY':.5},2.5)]:
            with self.assertRaises(ValueError):goal_rates(r,.5,line)

    def test_own_corner_price_and_labels_do_not_affect_anchor(self):
        q=self.quotes();before=anchors(q)[0];extra=dict(q[0],spec=['CORNERS','TOTAL','OVER','',9.5],odd=2.,actual=99)
        self.assertEqual(before,anchors(q+[extra])[0]);self.assertEqual(before,anchors([dict(r,actual=99,goals=50) for r in q])[0])

    def test_missing_stale_and_future_clocks_are_not_fabricated(self):
        for c in ({'marketUpdatedAtUTC':None},{'marketUpdatedAtUTC':300001.},{'marketUpdatedAtUTC':1.}):
            q=[dict(r,**c) for r in self.quotes()];self.assertIsNotNone(valid_quote(q[0]));self.assertFalse(anchors(q)[0])

    def test_receipts_ids_and_observation_clocks_are_not_mixed(self):
        for c in ({'sourceReceipt':'other'},{'providerEventId':'13'},{'observed':300001.}):
            q=self.quotes();q[-1]=dict(q[-1],**c);self.assertFalse(anchors(q)[0])

    def test_incomplete_bookmakers_do_not_complete_one_another(self):
        q=self.quotes();q[0]['bookmaker']='Unibet';self.assertFalse(anchors(q)[0])

    def test_book_aliases_collapse_and_input_order_does_not_change_anchor(self):
        q=self.quotes()+self.quotes('Bet365 (no latency)')+self.quotes('Unibet');r=anchors(q)[0]['f']
        self.assertEqual(r['realBookmakers'],2);self.assertEqual({v['bookmaker'] for v in r['books']},{'Bet365','Unibet'});self.assertEqual(r,anchors(list(reversed(q)))[0]['f'])

    def test_conflicting_outcome_is_not_silently_selected(self):
        q=self.quotes();q.append(dict(q[0],odd=q[0]['odd']+.2));self.assertFalse(anchors(q)[0])

class CrossMarketCountTests(unittest.TestCase):
    def records(self,n=160):
        rng=np.random.default_rng(19);rows=[]
        for i in range(n):
            s=float(i%2);mu=10.*math.exp(.1+.2*s);share=float(expit(.2+.3*s));total=int(rng.negative_binomial(20.,20./(20.+mu)))
            p=float(rng.beta(share*30,(1-share)*30));h=int(rng.binomial(total,p))
            rows.append(dict(fixture=str(i),kickoff=100000.+i,featureCutoff=0.,baseMeans=[5.,5.],x=[1.,s,s,.02,0.,0.,0.,.5],allocation=[1.,s],dispersion=[1.,s],actual=[h,total-h]))
        return rows

    def test_likelihood_matches_scipy_and_gradient_matches_finite_differences(self):
        rows=self.records(40);a=design(rows);w=np.full(40,1/40);t=np.r_[np.full(len(FEATURES),.02),math.log(.08),.1,.1,.15,math.log(25.)]
        loss,g=objective(t,a,w);pen=PENALTY*(float(t@t)-t[len(FEATURES)]**2-t[-1]**2)
        self.assertAlmostEqual(loss,-np.mean([joint_log_probability(r,dict(fitted=True,coef=t.tolist())) for r in rows])+pen,places=9)
        numerical=[]
        for i in range(len(t)):
            step=np.zeros(len(t));step[i]=1e-5;numerical.append((objective(t+step,a,w)[0]-objective(t-step,a,w)[0])/2e-5)
        np.testing.assert_allclose(g,numerical,atol=2e-6,rtol=2e-5)

    def test_fit_improves_prior_loss_and_static_ablation_has_no_dispersion_slope(self):
        rows=self.records();m=fit(rows,300000.);t=np.r_[np.zeros(len(FEATURES)),math.log(.05),0.,0.,0.,math.log(30.)]
        self.assertLess(m['loss'],objective(t,design(rows),np.full(len(rows),1/len(rows)))[0]);self.assertFalse(m['roiUsed']);self.assertGreater(m['trainingBefore'],m['maxTrainingKickoff']+86400)
        self.assertEqual(fit(rows,300000.,dynamic=False)['coef'][len(FEATURES)+1],0.)

    def test_duplicate_contracts_do_not_increase_fixture_weight(self):
        r=self.records();a=fit(r,300000.);b=fit(r+[copy.deepcopy(r[0]) for _ in range(10)],300000.)
        np.testing.assert_allclose(a['coef'],b['coef'],atol=2e-7,rtol=2e-7)

    def test_future_labels_cannot_change_prior_bank(self):
        r=self.records();f=dict(r[0],fixture='future',kickoff=299999.,actual=[99,99]);self.assertEqual(bank(r,300000.),bank(r+[f],300000.))
        with self.assertRaises(ValueError):fit(r+[f],300000.)

    def test_exact_distribution_preserves_means_and_all_win_push_partitions(self):
        r=self.records();m=fit(r,300000.);dist=distribution(r[0],m);p=parameters(r[0],m)
        self.assertAlmostEqual(float(dist.pmf.sum()),1.,places=12);np.testing.assert_allclose(dist.means,[p['home'],p['away']],atol=1e-7)
        for scope,team,line in [('TOTAL','',10.),('TEAM','HOME',5.5),('TEAM','AWAY',4.)]:
            w,push=dist.evaluate(('CORNERS',scope,'OVER',team,line));l,push2=dist.evaluate(('CORNERS',scope,'UNDER',team,line));self.assertAlmostEqual(push,push2);self.assertAlmostEqual(w+l+push,1.,places=10)

    def test_zero_counts_have_finite_loss_and_gradient(self):
        r=[dict(v,actual=[0,0]) for v in self.records(5)];t=np.r_[np.zeros(len(FEATURES)),math.log(.05),0.,0.,0.,math.log(30.)]
        value,g=objective(t,design(r),np.full(5,.2));self.assertTrue(np.isfinite(value));self.assertTrue(np.isfinite(g).all())

    def test_features_ignore_labels_and_enforce_lag(self):
        a=dict(home=1.7,away=1.1,drawResidual=.02,featureCutoff=172800.,observed=300000.,kickoff=400000.)
        c=dict(featureCutoff=172800.,corner_recent=10.,corner_venue=10.,sot_recent=8.,sot_home=10,sot_away=12)
        self.assertEqual(feature_vector(a,[5.,5.],c),feature_vector(dict(a,actual=90),[5.,5.],dict(c,actual=99)))
        with self.assertRaises(ValueError):feature_vector(a,[5.,5.],dict(c,featureCutoff=259200.))

    def test_tiny_samples_do_not_fit_or_predict(self):
        m=fit(self.records(99),300000.);self.assertFalse(m['fitted'])
        with self.assertRaises(ValueError):distribution(self.records(1)[0],m)

if __name__=='__main__':unittest.main()
