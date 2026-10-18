import copy,sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from betting_v2.gap_ratings import encode,roll,fit,state,predict,INITIAL

def values(r,m):return r.get(m)

class GapTests(unittest.TestCase):
    def rows(self,n=180):
        return [dict(_date=86400.*(i+1),_home='a' if i%2 else 'b',_away='b' if i%2 else 'a',_home_group='g',_away_group='g',
                     SHOTS=[16.+i%3,8.+i%2],SOT=[6.,3.],CORNERS=[7.,3.]) for i in range(n)]

    def test_first_match_updates_all_four_venue_ratings_from_one_shared_prediction(self):
        r=self.rows(1);e=encode(r,'CORNERS',values,200000.);p=roll(e,(.1,.75,.75));h=e['ids'][r[0]['_home']];a=e['ids'][r[0]['_away']]
        np.testing.assert_allclose(p['ratings'][h],[4.6875,4.3875,4.5625,4.4625])
        np.testing.assert_allclose(p['ratings'][a],[4.4625,4.5625,4.3875,4.6875])

    def test_same_day_order_cannot_change_predictions_or_ratings(self):
        r=self.rows(3);r[1]['_date']=r[0]['_date'];r[1]['_home']='c'
        a=encode(r,'SHOTS',values,400000.);b=encode(list(reversed(r)),'SHOTS',values,400000.)
        self.assertEqual(a['ids'],b['ids']);np.testing.assert_allclose(roll(a,INITIAL)['ratings'],roll(b,INITIAL)['ratings'])

    def test_future_labels_and_new_teams_cannot_change_lagged_state(self):
        r=self.rows(50);cutoff=86400.*30;params=dict(theta=list(INITIAL))
        before=state(r,'SHOTS',values,cutoff,params);future=dict(r[-1],_date=cutoff+1,_home='future',SHOTS=[99.,99.])
        self.assertEqual(before,state(r+[future],'SHOTS',values,cutoff,params))

    def test_initialization_is_prefix_only(self):
        r=self.rows(20);a=encode(r,'CORNERS',values,2000000.)
        changed=copy.deepcopy(r);changed[-1]['CORNERS']=[99.,99.];b=encode(changed,'CORNERS',values,2000000.)
        np.testing.assert_allclose(a['initial'],b['initial'])

    def test_duplicate_labels_are_rejected_and_missing_metric_is_skipped(self):
        r=self.rows(3)
        with self.assertRaises(ValueError):encode(r+[r[0]],'SHOTS',values,400000.)
        self.assertEqual(encode([dict(r[0],SHOTS=None)],'SHOTS',values,400000.)['fixtures'],0)

    def test_burn_in_is_based_only_on_previous_observed_matches(self):
        e=encode(self.rows(10),'SHOTS',values,1000000.);r=roll(e,INITIAL,forecasts=True)
        self.assertEqual(r['scoredFixtures'],4);self.assertFalse(r['forecasts'][5]['mature']);self.assertTrue(r['forecasts'][6]['mature'])

    def test_prior_fit_uses_no_goal_or_odds_fields_and_no_current_labels(self):
        r=self.rows();cutoff=86400.*190;a=fit(r,'CORNERS',values,cutoff)
        b=fit([dict(v,GOALS_FT=[99.,99.],odd=999.) for v in r]+[dict(r[-1],_date=cutoff-1,CORNERS=[99.,99.])],'CORNERS',values,cutoff)
        self.assertEqual(a,b);self.assertTrue(a['fitted']);self.assertFalse(a['roiUsed']);self.assertLessEqual(a['mae'],a['initialMae']+1e-8)

    def test_unknown_teams_have_explicit_cold_start_and_valid_forecast(self):
        s=state(self.rows(5),'SOT',values,600000.,dict(theta=list(INITIAL)));p=predict(s,'new','other')
        self.assertTrue(p['coldStart']);self.assertEqual(p['home'],4.);self.assertEqual(p['sampleHome'],0)

if __name__=='__main__':unittest.main()
