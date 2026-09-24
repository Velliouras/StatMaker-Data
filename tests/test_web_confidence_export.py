import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/web_confidence_uat'))
from export_confidence import prepare_inputs, read_only, tsv_row
from build_classifier import source

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.perf=self.root/'performance.db';self.prepared=self.root/'prepared.db'
        self.manifest={'contentVersion':'snapshot','metadata':{'preparedPatternGenerationId':'generation'}}
        with sqlite3.connect(self.perf) as db:
            db.execute('CREATE TABLE model_recommendations (id TEXT, created_at INTEGER, match_date TEXT, competition TEXT, league_code TEXT, market TEXT, odd REAL, family TEXT, sub_market_key TEXT, model_probability REAL, edge REAL, value_tier TEXT, outcome TEXT, settlement_return REAL, prediction_source TEXT, source_kind TEXT)')
            db.execute("INSERT INTO model_recommendations VALUES ('old',1,'2026-01-01','League','L','MATCH_GOALS',1.8,'MATCH_GOALS',NULL,.7,.1,'Strong Value','WON',1.8,'BOOKMAKER_POSTERIOR','PREPARED_FINAL_DAILY_V11')")
            db.execute("INSERT INTO model_recommendations SELECT 'legacy',0,match_date,competition,league_code,market,odd,family,sub_market_key,model_probability,edge,value_tier,outcome,settlement_return,prediction_source,'LEGACY' FROM model_recommendations WHERE id='old'")
        with sqlite3.connect(self.prepared) as db:
            db.executescript('''CREATE TABLE prepared_pattern_generation(generation_id TEXT,state TEXT);
CREATE TABLE prepared_pattern_candidates(generation_id TEXT, competition_id TEXT,snapshot_version TEXT,selection_key TEXT,selection_odd REAL,recommendation_eligible INTEGER,value_tier TEXT,market_family TEXT);
CREATE TABLE prepared_selections(competition_id TEXT,snapshot_version TEXT,selection_key TEXT,match_key TEXT,identity_family TEXT,selection_market TEXT,selection_name TEXT,opponent_model_probability REAL,bm_posterior_probability REAL,bm_market_probability REAL,bookmaker_evidence_payload TEXT,opponent_adjusted_required INTEGER);
CREATE TABLE prepared_matches(competition_id TEXT,snapshot_version TEXT,match_key TEXT,payload TEXT);
INSERT INTO prepared_pattern_generation VALUES ('generation','ready');
INSERT INTO prepared_pattern_candidates VALUES ('generation','domestic','snapshot','pick',1.8,1,'Strong Value','MATCH_GOALS');
INSERT INTO prepared_selections VALUES ('domestic','snapshot','pick','match','MATCH_GOALS','MATCH_GOALS','Over 2.5',NULL,.7,.6,'{}',0);
INSERT INTO prepared_matches VALUES ('domestic','snapshot','match','{"competition":"League","leagueCode":"L"}');''')
    def tearDown(self):self.tmp.cleanup()
    def prepare(self):return prepare_inputs(self.perf,self.prepared,self.manifest,'2026-09-23')
    def test_all_history_preserved_not_30_days(self):
        before=self.perf.read_bytes();entries,contexts,unavailable,count=self.prepare()
        self.assertEqual([x['id'] for x in entries],['old']);self.assertEqual(len(contexts),1)
        self.assertEqual(unavailable,[]);self.assertEqual(self.perf.read_bytes(),before)
    def test_missing_generation_fails(self):
        self.manifest['metadata']['preparedPatternGenerationId']='wrong'
        with self.assertRaisesRegex(ValueError,'generation'):self.prepare()
    def test_missing_runtime_context_not_developing(self):
        with sqlite3.connect(self.prepared) as db:db.execute('UPDATE prepared_selections SET opponent_adjusted_required=1')
        entries,contexts,unavailable,count=self.prepare()
        self.assertEqual(contexts,[]);self.assertEqual(unavailable[0]['status'],'unavailable')
        self.assertNotIn('tier',unavailable[0])
    def test_retired_and_low_odds_excluded(self):
        with sqlite3.connect(self.prepared) as db:db.execute("UPDATE prepared_pattern_candidates SET market_family='ASIAN_GOALS'")
        self.assertEqual(self.prepare()[3],0)
        with sqlite3.connect(self.prepared) as db:db.execute("UPDATE prepared_pattern_candidates SET market_family='MATCH_GOALS',selection_odd=1.49")
        self.assertEqual(self.prepare()[3],0)
    def test_wal_rejected(self):
        Path(str(self.perf)+'-wal').write_bytes(b'active')
        with self.assertRaisesRegex(ValueError,'WAL'):read_only(self.perf)
    def test_empty_canonical_history_fails(self):
        with sqlite3.connect(self.perf) as db:db.execute('DELETE FROM model_recommendations')
        with self.assertRaisesRegex(ValueError,'No canonical'):self.prepare()
    def test_unknown_outcome_fails(self):
        with sqlite3.connect(self.perf) as db:db.execute("UPDATE model_recommendations SET outcome='UNKNOWN'")
        with self.assertRaisesRegex(ValueError,'Unknown'):self.prepare()
    def test_transport_preserves_null(self):
        self.assertEqual(tsv_row([None,'',0]),'~\t\tMA==')
    def test_source_drift_fails(self):
        (self.root/'ModelPerformanceConfidence.kt').write_text('changed')
        with self.assertRaisesRegex(ValueError,'mismatch'):source(self.root,'ModelPerformanceConfidence.kt')

if __name__=='__main__':unittest.main()
