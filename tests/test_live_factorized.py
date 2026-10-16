import hashlib,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from betting_v2.live_factorized import load_factorized,read_bound
from betting_v2.opponent_matchup import OpponentMatchup,timestamp
from betting_v2.validated_walk_forward import distribution
from betting_v2.exact_forecast_adapter import forecast
from hotpatch_betting_v2_direct import table_fingerprints

class FactorizedLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch('betting_v2.monotone_count.fit_monotone_count',side_effect=AssertionError('No refit')):
            cls.model,cls.profile,cls.meta=load_factorized(ROOT,[],timestamp('2026-10-01'))
        cls.rows=json.loads((ROOT/'tests/fixtures/factorized_frozen_october.json').read_text())

    @staticmethod
    def pair(fixture):
        parts=fixture.split(':');return ':'.join(parts[1:3]),':'.join(parts[3:5])

    def test_reproduces_saved_research_for_all_nine_families(self):
        self.assertEqual(len({r['metric'] for r in self.rows}),9)
        for r in self.rows:
            prediction=self.model.predict(*self.pair(r['fixture']),r['metric'])
            self.assertIsNotNone(prediction)
            np.testing.assert_allclose(prediction[:2],r['means'],rtol=1e-12,atol=1e-12)
            self.assertEqual(list(prediction[2:4]),r['samples'])

    def test_frozen_profiles_preserve_probabilities_and_coherent_half_means(self):
        for r in self.rows:
            if r['metric']!='GOALS_FT':continue
            pair=self.pair(r['fixture']);full=self.model.predict(*pair,'GOALS_FT')
            first=self.model.predict(*pair,'GOALS_1H');second=self.model.predict(*pair,'GOALS_2H')
            if first is None or second is None:continue
            def value(metric,prediction):
                row=dict(metric=metric,regime=r['regime'],means=list(prediction[:2]))
                if metric!='GOALS_FT':row.update(fullMeans=list(full[:2]),shares=[prediction[i]/full[i] for i in (0,1)])
                return distribution(row,self.profile).means
            np.testing.assert_allclose(np.asarray(value('GOALS_1H',first))+value('GOALS_2H',second),
                                       value('GOALS_FT',full),atol=1e-10)

    def test_only_completed_days_after_saved_state_are_observed(self):
        cutoff=timestamp('2026-10-16');calls=[]
        rows=[dict(_date=timestamp('2026-09-30')),dict(_date=cutoff-1),dict(_date=cutoff)]
        with patch.object(OpponentMatchup,'observe',side_effect=lambda r,m:calls.append(r['_date'])):
            _,_,meta=load_factorized(ROOT,rows,cutoff)
        self.assertEqual(calls,[cutoff-1,cutoff-1]);self.assertEqual(meta['featureRowsRolled'],1)

    def test_past_source_corrections_do_not_retrain_frozen_state(self):
        model,_,_=load_factorized(ROOT,[dict(_date=timestamp('2026-08-01'),home_goals=99)],timestamp('2026-10-01'))
        r=self.rows[0]
        self.assertEqual(model.predict(*self.pair(r['fixture']),r['metric']),
                         self.model.predict(*self.pair(r['fixture']),r['metric']))

    def test_artifact_hash_mismatch_and_path_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'profile.json';p.write_text('{}')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                read_bound(root,dict(path='profile.json',sha256='0'*64))
            with self.assertRaisesRegex(ValueError,'outside repository'):
                read_bound(root,dict(path='../profile.json',sha256='0'*64))

    def test_cutoff_cannot_precede_selection_or_include_partial_day(self):
        for cutoff in [timestamp('2026-09-30'),timestamp('2026-10-16')+1]:
            with self.assertRaises(ValueError):load_factorized(ROOT,[],cutoff)

    def test_provenance_identifies_experimental_arm_without_profit_certification(self):
        self.assertEqual(self.meta['variant'],'separate_ft_selected_period')
        self.assertFalse(self.meta['profitabilityValidated']);self.assertFalse(self.meta['empiricalTiersValidated'])
        self.assertEqual(self.meta['providerCalls'],0)

    def test_table_fingerprint_detects_any_non_uat_change(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'db.sqlite'
            with sqlite3.connect(p) as con:
                con.execute('CREATE TABLE legacy(value)');con.execute('INSERT INTO legacy VALUES (123)')
                con.execute('CREATE TABLE prepared_betting_v4_forecasts(value)')
            before=table_fingerprints(p)
            with sqlite3.connect(p) as con:con.execute('INSERT INTO prepared_betting_v4_forecasts VALUES (5)')
            self.assertEqual(table_fingerprints(p),before)
            with sqlite3.connect(p) as con:con.execute('UPDATE legacy SET value=124')
            self.assertNotEqual(table_fingerprints(p),before)

if __name__=='__main__':unittest.main()
