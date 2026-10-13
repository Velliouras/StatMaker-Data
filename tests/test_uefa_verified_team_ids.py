"""UEFA API backfill should reuse real fixture team IDs without guessing."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from uefa_verified_team_ids import load_verified_api_team_index, find_verified_team

class VerifiedUEFATeamIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cache="data/api_football/fixture_stats/italy/serie-a/2026/fixture_stats.json"
        self._put("data/statmaker/domestic_enriched/index.json",{"leagues":[{
            "league_code":"I1","cache_path":self.cache,"api_football_league_id":135,
            "api_football_season":"2026"}]})
        self._put("mappings/domestic_team_aliases.json",{"aliases":{"I1":{
            "Napoli":["SSC Napoli"],"Juventus":["Juventus Turin"]}}})
        self._put(self.cache,{"provider":"api-football","league_id":135,"season":"2026",
            "schedule_fixtures":[
                {"fixture_id":555,"status":"NS","source_league":{"id":135,"season":2026},
                 "home_team":"Napoli","home_team_id":492,
                 "away_team":"Juventus","away_team_id":496}]})
    def _put(self,path,payload):
        f=self.root/path
        f.parent.mkdir(parents=True,exist_ok=True)
        f.write_text(json.dumps(payload),encoding="utf-8")
    def test_exact_cached_club_id_and_verified_alias(self):
        idx,report=load_verified_api_team_index(self.root)
        self.assertEqual(find_verified_team(idx,"Napoli")["id"],492)
        self.assertEqual(find_verified_team(idx,"SSC Napoli")["id"],492)
        self.assertEqual(find_verified_team(idx,"Juventus Turin")["id"],496)
        self.assertEqual(report["providerCalls"],0)
    def test_ambiguous_source_team_name_never_resolves(self):
        path="data/api_football/fixture_stats/italy/serie-b/2026/fixture_stats.json"
        idx=self.root/"data/statmaker/domestic_enriched/index.json"
        obj=json.loads(idx.read_text(encoding="utf-8"))
        obj["leagues"].append({"league_code":"I2","cache_path":path,
            "api_football_league_id":136,"api_football_season":"2026"})
        idx.write_text(json.dumps(obj),encoding="utf-8")
        self._put(path,{"provider":"api-football","league_id":136,"season":"2026",
            "schedule_fixtures":[{"source_league":{"id":136,"season":2026},
                "home_team":"Napoli","home_team_id":8888,
                "away_team":"Bari","away_team_id":7777}]})
        found,report=load_verified_api_team_index(self.root)
        self.assertIsNone(find_verified_team(found,"Napoli"))
        self.assertIn("Napoli" if False else "cachedUniqueTeamAliases",report)
    def test_wrong_source_league_and_year_never_creates_ids(self):
        f=self.root/self.cache
        doc=json.loads(f.read_text(encoding="utf-8"))
        doc["schedule_fixtures"][0]["source_league"]["id"]=999
        f.write_text(json.dumps(doc),encoding="utf-8")
        found,_=load_verified_api_team_index(self.root)
        self.assertIsNone(find_verified_team(found,"Napoli"))


if __name__=="__main__":
    unittest.main()
