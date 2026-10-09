import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.package_uat_prepared_from_prod_snapshot import pack, sha_file


class IsolatedRepackageTests(unittest.TestCase):
    RULES="test-exact-rules"

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.prepared=self.root/"uat.db"
        db=sqlite3.connect(self.prepared)
        db.execute("PRAGMA user_version=12")
        db.execute("CREATE TABLE prepared_pattern_generation(generation_id TEXT,source_fingerprint TEXT,rules_fingerprint TEXT,candidate_count INTEGER,state TEXT)")
        db.execute("INSERT INTO prepared_pattern_generation VALUES ('gen','src',?,1,'ready')",(self.RULES,))
        db.execute("CREATE TABLE prepared_pattern_candidates(generation_id TEXT, competition_id TEXT, snapshot_version TEXT, selection_key TEXT,recommendation_eligible INT,policy_premium_eligible INT)")
        db.execute("INSERT INTO prepared_pattern_candidates VALUES ('gen','domestic','snap','sel',1,1)")
        db.execute("CREATE TABLE prepared_selections(competition_id TEXT,snapshot_version TEXT,selection_key TEXT,identity_sub_market_key TEXT,bm_market_probability_source TEXT,bm_market_overround REAL,opponent_model_probability REAL,opponent_adjusted_required INT)")
        db.execute("INSERT INTO prepared_selections VALUES ('domestic','snap','sel','RESULT_1X2','no-vig',1.05,.62,1)")
        db.commit();db.close()
        self.source={}
        for kind, content in [('stats',{'databases/statmaker.db':b'original-stats'}),
                              ('betting',{'databases/statmaker_prepared_betting.db':b'original-betting','files/app_ready_odds/domestic.json':b'original-odds'})]:
            manifest={'schemaVersion':1,'bundleType':kind,'files':[
                {'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
                for name,data in sorted(content.items())]}
            name='app_ready_stats_bundle' if kind=='stats' else 'app_ready_betting_bundle'
            path=self.root/(name+'-original.zip')
            with zipfile.ZipFile(path,'w') as archive:
                for key,value in content.items():archive.writestr(key,value)
                archive.writestr('bundle_manifest.json',json.dumps(manifest))
            self.source[name]={'id':name,'path':path.name,'bytes':path.stat().st_size,
                'sha256':sha_file(path),'group':'read_model' if kind=='stats' else 'prepared'}
        self.manifest={'contentVersion':'base','metadata':{'mainContentVersion':'main','uefaContentVersion':'uefa'},'artifacts':list(self.source.values())}

    def tearDown(self):self.tmp.cleanup()

    def test_isolated_repack_does_not_change_original_market_bytes(self):
        target='data/statmaker/app_ready_uat/probability-first-v1'
        m=pack(self.root,self.manifest,self.prepared,self.root/'out',target,'mysha',self.RULES)
        self.assertEqual(m['profile'],'app_ready_uat')
        self.assertEqual(m['metadata']['statmakerCommit'],'mysha')
        self.assertEqual(m['metadata']['preparedPatternCandidateCount'],1)
        self.assertEqual(m['metadata']['preparedPatternRulesFingerprint'],self.RULES)
        self.assertEqual(m['metadata']['inputOddsProvenance'],'immutable-published-PROD-App-Ready-betting-archive')
        z=next((self.root/'out').glob('app_ready_betting_bundle-*.zip'))
        with zipfile.ZipFile(z) as archive:
            self.assertEqual(archive.read('files/app_ready_odds/domestic.json'),b'original-odds')
            self.assertEqual(archive.read('databases/statmaker_prepared_betting.db'),self.prepared.read_bytes())
        self.assertEqual(sha_file(self.root/self.source['app_ready_stats_bundle']['path']),sha_file(self.root/'out'/self.source['app_ready_stats_bundle']['path']))

    def test_refuses_prod_target_and_corrupt_sha(self):
        with self.assertRaisesRegex(ValueError,'PROD'):
            pack(self.root,self.manifest,self.prepared,self.root/'out','data/statmaker/app_ready','sha',self.RULES)
        self.manifest['artifacts'][0]['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'SHA mismatch'):
            pack(self.root,self.manifest,self.prepared,self.root/'out','data/statmaker/app_ready_uat/test','sha',self.RULES)

    def test_wrong_rules_refused(self):
        with self.assertRaisesRegex(ValueError,'rules'):
            pack(self.root,self.manifest,self.prepared,self.root/'out','data/statmaker/app_ready_uat/test','sha','wrong')

    def test_old_overlapping_double_chance_candidate_refused(self):
        db=sqlite3.connect(self.prepared)
        db.execute("UPDATE prepared_selections SET identity_sub_market_key='RESULT_DOUBLE_CHANCE',bm_market_overround=2.07")
        db.commit();db.close()
        with self.assertRaisesRegex(ValueError,'Stale double-chance'):
            pack(self.root,self.manifest,self.prepared,self.root/'out','data/statmaker/app_ready_uat/test','sha',self.RULES)

if __name__=='__main__':unittest.main()
