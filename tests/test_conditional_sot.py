import copy,math,sys,unittest
from pathlib import Path
import numpy as np
from scipy.special import expit,logit
from scipy.stats import betabinom,binom
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.conditional_sot import ConversionHistory,paired_actual,transition,distribution,design,objective,fit,bank,parameters,PENALTY
from betting_v2.joint_count import JointCount
from betting_v2.research_conditional_sot import feature_frame,scores
from betting_v2.research_context_market import eligible,conservative_winner

def metric(row,name):return row.get(name)

class ConditionalMathTests(unittest.TestCase):
    def records(self,n=160):
        rng=np.random.default_rng(37);rows=[]
        for i in range(n):
            shots=rng.integers(5,30,size=2);p=expit(logit(np.array([.32,.30]))+.15+np.array([.04,-.04]));sot=rng.binomial(shots,p)
            rows.append(dict(fixture=str(i),kickoff=100000.+i,featureCutoff=0.,maxObservedKickoff=None,shots=shots.tolist(),sot=sot.tolist(),baseConversion=[.32,.30]))
        return rows
    def test_likelihood_and_gradient_match_independent_references(self):
        r=self.records(35);a=design(r);w=np.full(70,1/70);t=np.array([.12,-.08,math.log(24.)]);loss,g=objective(t,a,w)
        n,y,offset,x=a;p=expit(offset+x@t[:2]);k=math.exp(t[2])
        expected=-float(w@betabinom.logpmf(y,n,p*k,(1-p)*k))+PENALTY*float(t[:2]@t[:2]);self.assertAlmostEqual(loss,expected,places=10)
        numerical=[]
        for i in range(3):
            step=np.zeros(3);step[i]=1e-5;numerical.append((objective(t+step,a,w)[0]-objective(t-step,a,w)[0])/2e-5)
        np.testing.assert_allclose(g,numerical,atol=2e-7,rtol=2e-5)
    def test_transition_is_stochastic_and_cannot_exceed_shots(self):
        t=transition(61,.34,20.)
        np.testing.assert_allclose(t.sum(axis=1),1.,atol=1e-13);self.assertEqual(float(np.triu(t,1).sum()),0.)
        np.testing.assert_allclose(t@np.arange(61),np.arange(61)*.34,atol=1e-10)
    def test_large_concentration_approaches_binomial(self):
        t=transition(21,.37,1e7);np.testing.assert_allclose(t[20],binom.pmf(np.arange(21),20,.37),atol=4e-7)
    def test_parent_transform_preserves_expected_means_and_tail_bound(self):
        parent=JointCount.from_means(13.,10.,alpha=.04);r=self.records(1)[0];m=dict(fitted=True,coef=[.1,.05,math.log(30.)]);new=distribution(parent,r,m);p,_=parameters(r,m)
        np.testing.assert_allclose(new.means,np.array(parent.means)*p,atol=1e-8);self.assertEqual(new.omitted_mass,parent.omitted_mass)
        for scope,team,line in [('TOTAL','',8.),('TEAM','HOME',4.5),('TEAM','AWAY',3.)]:
            w,push=new.evaluate(('SOT',scope,'OVER',team,line));l,push2=new.evaluate(('SOT',scope,'UNDER',team,line));self.assertAlmostEqual(w+l+push,1.);self.assertAlmostEqual(push,push2)
    def test_zero_shots_have_finite_loss_zero_gradient(self):
        rows=[dict(r,shots=[0,0],sot=[0,0]) for r in self.records(5)];loss,g=objective(np.array([0.,0.,math.log(30.)]),design(rows),np.full(10,.1));self.assertEqual(loss,0.);np.testing.assert_array_equal(g,np.zeros(3))
    def test_fit_improves_likelihood_without_prices_or_roi(self):
        r=self.records();m=fit(r,300000.);self.assertTrue(m['fitted']);self.assertFalse(m['roiUsed']);self.assertLess(m['loss'],objective(np.array([0.,0.,math.log(30.)]),design(r),np.full(320,1/320))[0])
        changed=[dict(x,odd=999,profit=-999) for x in r];self.assertEqual(m,fit(changed,300000.))
    def test_future_label_is_filtered_and_boundary_violation_rejected(self):
        r=self.records(100);f=dict(r[0],fixture='future',kickoff=299999.,sot=[0,0]);self.assertEqual(bank(r,300000.),bank(r+[f],300000.))
        with self.assertRaises(ValueError):fit(r+[f],300000.)
        with self.assertRaises(ValueError):fit([dict(r[0],maxObservedKickoff=0.)],300000.)
    def test_duplicate_fixture_and_invalid_counts_are_rejected(self):
        r=self.records()
        with self.assertRaises(ValueError):fit(r+[copy.deepcopy(r[0])],300000.)
        for extra in [dict(sot=[100,0]),dict(shots=[-1,12]),dict(sot=[.5,2])]:
            with self.assertRaises(ValueError):design([dict(r[0],**extra)])
    def test_tiny_fit_cannot_be_used(self):
        r=self.records(99);m=fit(r,300000.);self.assertFalse(m['fitted'])
        with self.assertRaises((ValueError,KeyError)):distribution(JointCount.from_means(10,10),r[0],m)

class ConversionClockTests(unittest.TestCase):
    def history(self):
        return [dict(_home='h',_away='a',_home_group='g',_away_group='g',_date=d,SHOTS=[12,9],SOT=s) for d,s in [(86400.,[5,2]),(172800.,[2,4]),(259200.,[12,9])]]
    def test_prediction_ignores_future_and_same_day_labels(self):
        h=self.history();s=ConversionHistory(h,metric);s.advance(172800.);before=s.features('h','a')
        changed=[dict(r,SOT=[0,0]) if r['_date']>=172800 else r for r in h];other=ConversionHistory(changed,metric);other.advance(172800.);self.assertEqual(before,other.features('h','a'));self.assertEqual(before['maxObservedKickoff'],86400.)
        s.advance(259200.);self.assertNotEqual(before['baseConversion'],s.features('h','a')['baseConversion'])
    def test_cold_start_uses_only_fixed_and_prefix_priors(self):
        s=ConversionHistory(self.history(),metric);s.advance(0.);f=s.features('newh','newa');np.testing.assert_allclose(f['baseConversion'],[1/3,1/3]);self.assertIsNone(f['maxObservedKickoff'])
        s.advance(172800.);self.assertNotEqual(s.features('newh','newa')['baseConversion'],f['baseConversion'])
    def test_duplicate_or_impossible_history_and_backwards_clock_fail(self):
        h=self.history()
        with self.assertRaises(ValueError):ConversionHistory(h+[h[0]],metric)
        with self.assertRaises(ValueError):paired_actual(dict(SHOTS=[1,1],SOT=[2,1]),metric)
        s=ConversionHistory(h,metric);s.advance(172800.)
        with self.assertRaises(ValueError):s.advance(86400.)
    def test_missing_paired_metric_is_skipped(self):
        h=self.history();h[0].pop('SOT');s=ConversionHistory(h,metric);s.advance(172800.);self.assertIsNone(s.features('h','a')['maxObservedKickoff'])

class ReplayTests(unittest.TestCase):
    def test_three_state_proper_scores_weight_fixtures_equally(self):
        a=dict(fixture='a',y=1.,push=0.,probabilities={k:[.6,.1] for k in ('stat','market','conditional')});b=dict(fixture='b',y=0.,push=1.,probabilities={k:[.3,.4] for k in ('stat','market','conditional')})
        self.assertAlmostEqual(scores([a,b])['conditional']['logLoss'],scores([a,a,a,b])['conditional']['logLoss']);self.assertEqual(scores([a,b])['conditionalMinusStat']['mean'],0.)
    def test_conservative_rejects_unchanged_floor_and_push_ev(self):
        q=dict(p=.65,push=0.,odd=1.9,spec=['SOT','TEAM','OVER','HOME',4.5],_lower=dict(p=.59,push=0.,support=.7))
        self.assertIsNone(conservative_winner(q));q['_lower']['p']=.61;q['_lower']['support']=.59;self.assertIsNone(conservative_winner(q))
        q['_lower']['support']=.7;self.assertIsNotNone(conservative_winner(q))

if __name__=='__main__':unittest.main()
