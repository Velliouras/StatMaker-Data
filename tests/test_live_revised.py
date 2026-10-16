import gzip,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from betting_v2.live_factorized import load_factorized
from betting_v2.live_revised import RevisedModel
from betting_v2.reliable_goals import ReliableGoals
from betting_v2.exact_forecast_adapter import forecast
from betting_v2.validated_walk_forward import distribution
from betting_v2.opponent_matchup import timestamp
from betting_v2.live_exact import run
import materialize_betting_v2_direct as direct
from hotpatch_betting_v2_direct import table_fingerprints


class RevisedLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory=ROOT/'reports/betting_v2/model_revision_20261016'
        def read(name):
            p=directory/name;raw=p.read_bytes();return json.loads(gzip.decompress(raw) if p.suffix=='.gz' else raw)
        base,old,meta=load_factorized(ROOT,[],timestamp('2026-10-01'))
        ft=ReliableGoals.restore(read('recency_2026-10-01_pretest_state.json.gz'))
        cal=read('recency_2026-10-01_selected_calibration.json')
        profile={k:v for k,v in old.items() if not k.startswith('GOALS')}
        profile.update(read('recency_2026-10-01_profile.json'));profile['_selected_joint']=cal['point']['theta']
        cls.base=base;cls.profile=profile;cls.model=RevisedModel(ft,base,profile,cal,.1)
        rows=read('recency_2026-10-01_predictions.json.gz')
        for row in rows:
            if row['featureCutoff']!=timestamp('2026-10-01'):continue
            parts=row['fixture'].split(':');pair=(':'.join(parts[1:3]),':'.join(parts[3:5]))
            if all(cls.model.predict(*pair,m) is not None for m in ('GOALS_FT','GOALS_1H','GOALS_2H','CORNERS')):
                cls.pair=pair;cls.row=row;break
        else:raise AssertionError('No complete October first-day fixture')

    def test_saved_revised_core_is_reproduced_without_refit(self):
        with patch('betting_v2.reliable_goals.weighted_fit',side_effect=AssertionError('No live fit')):
            np.testing.assert_allclose(self.model.predict(*self.pair,'GOALS_FT')[:2],self.row['means'],atol=1e-12,rtol=1e-12)

    def test_other_count_weights_remain_exactly_their_frozen_components(self):
        self.assertEqual(self.model.predict(*self.pair,'CORNERS'),self.base.predict(*self.pair,'CORNERS'))
        self.assertIsNone(self.model.bounds(*self.pair,('CORNERS','TOTAL','OVER','',9.5),2.))

    def test_calibrated_goal_period_means_remain_additive(self):
        values=[]
        for metric in ('GOALS_FT','GOALS_1H','GOALS_2H'):
            row,profile,prepared=self.model.prepared_bank(*self.pair,metric)[0]
            values.append(np.asarray(prepared[0].means))
        np.testing.assert_allclose(values[1]+values[2],values[0],atol=1e-10)

    def test_dnb_push_and_decision_ev_are_separate_and_consistent(self):
        for metric in ('GOALS_FT','GOALS_1H'):
            for scope,side,line in [('DNB','HOME',None),('TOTAL','UNDER',2.5)]:
                spec=(metric,scope,side,'',line);odd=2.2
                row,p,prepared=self.model.prepared_bank(*self.pair,metric)[0]
                point=forecast(row,p,spec,prepared);bound=self.model.bounds(*self.pair,spec,odd)
                self.assertLessEqual(bound['win'],point['winProbability'])
                self.assertLessEqual(bound['push'],point['pushProbability'])
                self.assertAlmostEqual(bound['ev'],bound['win']*odd+bound['push']-1)
                self.assertLessEqual(bound['ev'],point['winProbability']*odd+point['pushProbability']-1+1e-12)

    def test_sqlite_keeps_point_forecast_and_decision_bounds_in_distinct_columns(self):
        fields='competition_id snapshot_version match_key market_ordinal market selection team line odd win_probability push_probability prediction_model metric_key actual_runs home_history_sample away_history_sample league_training_sample prediction_home_mean prediction_away_mean match_home_win_probability match_away_win_probability home_scenario_probability away_scenario_probability home_scenario_sample away_scenario_sample'.split()
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/'db.sqlite'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE prepared_betting_v2_forecasts ('+','.join(fields)+')')
                con.execute('CREATE TABLE prepared_fixture_matches (competition_id,snapshot_version,match_key,league_code,kickoff,date,canonical_home_team,canonical_away_team,home_team,away_team,provider_home_team,provider_away_team,usable_for_stats,id,team_mapping_status)')
                con.execute('INSERT INTO prepared_fixture_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('domestic','s','f','E0','2026-10-02T12:00:00Z','2026-10-02','Home','Away','Home','Away','Home','Away',1,'1','matched'))
                con.execute('CREATE TABLE prepared_betting_v2_quotes (competition_id,snapshot_version,match_key,ordinal,market,selection,team,line,odd)')
                con.execute('INSERT INTO prepared_betting_v2_quotes VALUES (?,?,?,?,?,?,?,?,?)',
                    ('domestic','s','f',0,'MATCH_GOALS','Over',None,2.5,2.1))
            before=table_fingerprints(db)
            meta=dict(variant='test-revised',providerCalls=0)
            with patch('betting_v2.live_revised.load_revised',return_value=(self.model,self.profile,meta)),\
                 patch.object(direct,'load_actual_results',return_value={'GLOBAL':[]}),\
                 patch.dict(direct.UEFA_ALIASES,{'E0':{'home':self.pair[0],'away':self.pair[1]}}):
                result=run(db,ROOT,as_of=timestamp('2026-10-01'))
            self.assertEqual(result['uncertaintyForecasts'],1)
            self.assertEqual(table_fingerprints(db),before)
            with sqlite3.connect(db) as con:
                row=con.execute('SELECT win_probability,push_probability,odd,decision_win_probability,decision_push_probability,decision_ev FROM prepared_betting_v4_forecasts').fetchone()
            self.assertEqual(len(row),6);self.assertLessEqual(row[3],row[0]);self.assertLessEqual(row[4],row[1])
            self.assertAlmostEqual(row[5],row[3]*row[2]+row[4]-1)


if __name__=='__main__':unittest.main()
