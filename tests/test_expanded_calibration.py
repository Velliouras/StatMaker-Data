import sys,unittest,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.expanded_calibration import training_frame,fit_bank,adjusted,arms,proper_scores

class ExpandedCalibrationTests(unittest.TestCase):
    def fit(self,theta=[0.,1.]):
        return dict(point=dict(fitted=True,theta=theta),replicas=[dict(fitted=True,theta=theta) for _ in range(12)])
    def q(self):
        return dict(fixture='100000:h:a',spec=['SHOTS','TEAM','OVER','HOME',9.5],p=.75,push=.05,odd=2.,ev=.55,confidence=.75,preference=0,bookmaker='A',_lower=dict(p=.7,push=.03,support=.7))
    def proper(self):
        return [dict(fixture=f'{100000+i}:h:a',metric='SHOTS',fold='2026-08-01',y=float(i%2),push=0.,probabilities={'coupled':[.7,.1]}) for i in range(40)]
    def test_training_uses_all_canonical_events_and_excludes_only_actual_push(self):
        rows=self.proper();rows.append(dict(rows[0],push=1.,y=0.));frame=training_frame(rows,'coupled')
        self.assertEqual(len(frame),40);self.assertTrue(all(r['scope']=='ALL' for r in frame));self.assertAlmostEqual(frame[0]['pOver'],.7/.9)
    def test_future_labels_never_change_frozen_bank(self):
        frame=training_frame(self.proper(),'coupled');expected=fit_bank(frame,'SHOTS',300000.)
        future=dict(frame[0],fixture='future',kickoff=299999.)
        self.assertEqual(fit_bank(frame+[future],'SHOTS',300000.),expected)
    def test_identity_preserves_probabilities_and_lower_push(self):
        q=self.q();r=adjusted(q,self.fit());self.assertAlmostEqual(r['p'],q['p']);self.assertAlmostEqual(r['_lower']['p'],q['_lower']['p']);self.assertEqual(r['_lower']['push'],.03)
    def test_calibration_cannot_raise_decision_or_model_lower(self):
        q=self.q();r=adjusted(q,self.fit([1.,1.]));self.assertLessEqual(r['p'],q['p']);self.assertLessEqual(r['_lower']['p'],q['_lower']['p']);self.assertEqual(r['_lower']['support'],.7)
    def test_unsupported_prior_and_downward_gate_abstain(self):
        self.assertIsNone(adjusted(self.q(),dict(point=dict(fitted=False))))
        self.assertIsNone(adjusted(self.q(),self.fit([-3.,1.])))
    def test_point_winner_then_conservative_abstention_without_reranking(self):
        a=self.q();a['_lower']['p']=.59;b=copy.deepcopy(self.q());b.update(p=.7,confidence=.7);b['spec'][4]=10.5
        result=arms([a,b]);self.assertEqual(result['point'],[a]);self.assertEqual(result['conservative'],[])
    def test_full_partition_scores_include_push_and_identity_is_equal(self):
        rows=self.proper();rows.append(dict(rows[0],push=1.,y=0.));s=proper_scores(rows,'coupled',{'SHOTS':self.fit()})
        self.assertEqual(s['events'],41);self.assertEqual(s['fixtures'],40);self.assertAlmostEqual(s['raw']['logLoss'],s['calibrated']['logLoss']);self.assertAlmostEqual(s['calibratedMinusRawLogLoss'],0.)
    def test_invalid_source_probability_is_rejected(self):
        rows=self.proper();rows[0]['probabilities']['coupled']=[.9,.2]
        with self.assertRaises(ValueError):training_frame(rows,'coupled')

if __name__=='__main__':unittest.main()
