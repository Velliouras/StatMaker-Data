"""Real-source schema constraints for Phase-1 UEFA score history (offline)."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from collections import defaultdict

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from phase1_uefa_history import import_uefa_history


class UEFAHistoryPhase1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.groups={}
        self.aliases=defaultdict(lambda:defaultdict(set))
        self.current={"schemaVersion":1,"season":2026,"competitions":[
            {"leagueCode":"CL","season":2026,"fixtures":[
                {"fixtureId":900,"status":"FT","kickoff":"2026-09-01T18:00:00Z",
                 "homeTeam":"Team A","awayTeam":"Team B","providerHomeTeam":"Team A",
                 "providerAwayTeam":"Team B","homeGoals":2,"awayGoals":0,
                 "normalizedStats":{"HC":9,"AC":4,"HxG":1.4,"AxG":0.3}},
                {"fixtureId":901,"status":"NS","kickoff":"2026-11-01T18:00:00Z",
                 "homeTeam":"Team A","awayTeam":"Team B",
                 "homeGoals":3,"awayGoals":0,"normalizedStats":{}}]},
            {"leagueCode":"EL","season":2026,"fixtures":[]},
            {"leagueCode":"CONF","season":2026,"fixtures":[]}
        ]}
        self.support={"schemaVersion":1,"participants":[
            {"providerTeam":"Team A","canonicalTeam":"Team A","aliases":["Team A FC"],
             "matches":[
                {"competition":"UEFA Champions League","season":"2026","date":"2026-09-01",
                 "homeTeam":"Team A","awayTeam":"Team B","homeGoals":2,"awayGoals":0},
                {"competition":"UEFA Europa Conference League","season":"2026","date":"2026-07-01",
                 "homeTeam":"Team A","awayTeam":"Team C","homeGoals":1,"awayGoals":1},
                {"competition":"UEFA Champions League","season":"2025","date":"2025-03-01",
                 "homeTeam":"Team A","awayTeam":"Team C","homeGoals":4,"awayGoals":1}
             ]}]}
        self._put("data/statmaker/uefa_current_stats.json",self.current)
        self._put("data/statmaker/uefa_team_support_history.json",self.support)

    def _put(self,path,content):
        p=self.root/path
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(content),encoding="utf-8")

    def test_separate_uefa_competitions_no_cross_league_contamination(self):
        rows,trace=import_uefa_history(self.root,self.groups,self.aliases)
        self.assertEqual(len(rows),2)
        self.assertEqual({r["group"].split("|")[0] for r in rows},{"CL","UECL"})
        self.assertEqual(trace["totalHistoricalUEFARows"],2)
        self.assertEqual(trace["withMatchStats"],1)
        self.assertTrue(all(r["date"].year==2026 for r in rows))

    def test_current_fixture_preferred_over_duplicate_support(self):
        rows,trace=import_uefa_history(self.root,self.groups,self.aliases)
        cl=next(r for r in rows if r["group"].startswith("CL|"))
        self.assertEqual(cl["fixture_id"],"900")
        self.assertTrue(cl["apiFixtureIdentityVerified"])
        self.assertEqual(cl["stats"]["HC"],9)
        self.assertEqual(cl["hg"],2)

    def test_conflicting_uefa_support_scores_fail_closed(self):
        self.support["participants"][0]["matches"].append(
            {"competition":"UEFA Champions League","season":"2026","date":"2026-09-01",
             "homeTeam":"Team A","awayTeam":"Team B","homeGoals":4,"awayGoals":0})
        self._put("data/statmaker/uefa_team_support_history.json",self.support)
        rows,trace=import_uefa_history(self.root,self.groups,self.aliases)
        self.assertFalse(any(r["group"].startswith("CL|") for r in rows))
        self.assertEqual(trace["counters"]["CONFLICTING_UEFA_SCORE_SOURCE"],1)

    def test_missing_real_result_never_used(self):
        self.current["competitions"][0]["fixtures"][0]["homeGoals"]=None
        self.support["participants"][0]["matches"]=[]
        self._put("data/statmaker/uefa_current_stats.json",self.current)
        self._put("data/statmaker/uefa_team_support_history.json",self.support)
        rows,trace=import_uefa_history(self.root,self.groups,self.aliases)
        self.assertEqual(rows,[])
        self.assertEqual(trace["totalHistoricalUEFARows"],0)


if __name__=="__main__":
    unittest.main()
