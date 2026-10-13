"""Phase-1 archive reconciliation and European domestic-form guards."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, date, timezone
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts/betting_v2"))
from phase1_broad_support import archive_entries
from phase1_uefa_domestic_form import evaluate_uefa_form


class Phase1HistoricalSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/"data/statmaker/uefa_support_history.json"
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.samples=[
            {"competition":"Premier League","season":"2025","date":"2025-05-10",
             "homeTeam":"Team Alpha","awayTeam":"Team Beta","homeGoals":2,"awayGoals":1,
             "homeCorners":0,"awayCorners":4,"homeYellowCards":1,"awayYellowCards":2,
             "sourceLabel":"StatMaker verified API-Football domestic history"}]
        self.participants=[{"canonicalTeam":"Team Alpha","providerTeam":"Team Alpha",
                            "aliases":["Team Alpha"],"leagueCode":"E0","matches":self.samples}]
        self._save()
    def _save(self):
        self.path.write_text(json.dumps({"schemaVersion":1,"participants":self.participants}),encoding="utf-8")
    @staticmethod
    def norm(code,name):
        return str(name or "").lower()
    def test_real_zero_corners_are_valid_and_scores_unique(self):
        matches, report=archive_entries(self.root,{"E0|Premier League":{}},{},[],self.norm)
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0]["stats"]["HC"],0)
        self.assertTrue(matches[0]["fixture_id"].startswith("support-archive:"))
        self.assertEqual(report["counters"]["ARCHIVE_VERIFIED_HISTORICAL_ROWS_ADDED"],1)
    def test_do_not_double_count_score_already_canonical(self):
        existing=[{"group":"E0|Premier League","fixture_id":"provider-42",
                    "date":datetime(2025,5,10,15,tzinfo=timezone.utc),
                    "home":"team alpha","away":"team beta","hg":2,"ag":1,"stats":{"HC":None}}]
        after,report=archive_entries(self.root,{"E0|Premier League":{}},{},existing,self.norm)
        self.assertEqual(len(after),1)
        self.assertEqual(after[0]["fixture_id"],"provider-42")
        self.assertEqual(after[0]["stats"]["HC"],0)
    def test_conflicting_score_is_not_used(self):
        self.samples.append({**self.samples[0],"homeGoals":5})
        self._save()
        after,report=archive_entries(self.root,{"E0|Premier League":{}},{},[],self.norm)
        self.assertFalse(after)
        self.assertEqual(report["counters"]["CONFLICTING_ARCHIVED_SCORE"],1)
    def test_archive_cannot_relabel_domestic_as_uefa(self):
        matches,report=archive_entries(self.root,{"CL|UEFA Champions League":{}},{},[],self.norm)
        self.assertFalse(matches)
    def test_uefa_form_uses_only_prior_observed_scores(self):
        self.samples[:]=[
            {**self.samples[0],"date":f"2025-05-{i:02d}"}
            for i in range(1,11)]
        self._save()
        result=evaluate_uefa_form(self.root,[{
            "reason":"NO_VERIFIED_UEFA_TEAM_HISTORY","fixtureId":"x",
            "leagueCode":"CL","homeTeam":"Team Alpha","awayTeam":"Team Beta"}],
            date(2025,5,11))
        self.assertEqual(result["fixtureCount"],1)
        self.assertTrue(result["fixtures"][0]["sides"]["homeTeam"]["hasMinimumGoalForm"])
        self.assertFalse(result["fixtures"][0]["verifiedGoalFormForBoth"])
        self.assertFalse(result["fixtures"][0]["eligibleForUEFAPrediction"])


if __name__=="__main__":
    unittest.main()
