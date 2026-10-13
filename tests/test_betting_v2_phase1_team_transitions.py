from datetime import datetime,timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts/betting_v2"))
from phase1_team_transitions import cross_division_evidence,api_team_id


class VerifiedDivisionTransitionTests(unittest.TestCase):
    def test_previous_lower_division_is_visible_but_not_fictional_home_division_form(self):
        d=datetime(2026,5,1,12,tzinfo=timezone.utc)
        history=[{"group":"I2|Serie B","date":d,"fixture_id":"100",
                  "home":"api-team:136:11","away":"api-team:136:22",
                  "providerHomeId":"11","providerAwayId":"22","hg":1,"ag":0}]
        fixture={"group":"I1|Serie A","fixtureId":"upcoming","leagueCode":"I1",
                 "date":datetime(2026,10,14,19,tzinfo=timezone.utc),
                 "home":"api-team:135:11","away":"api-team:135:22",
                 "homeTeam":"A","awayTeam":"B"}
        result=cross_division_evidence(history,[fixture],
            datetime(2026,10,13,11,tzinfo=timezone.utc))
        self.assertEqual(result["checkedFixtures"],1)
        self.assertEqual(result["fixtures"][0]["sides"]["home"]["otherCompetitionMatches"],1)
        self.assertEqual(result["fixtures"][0]["sides"]["away"]["otherCompetitionMatches"],1)
        self.assertTrue(result["fixtures"][0]["calibrationRequired"])
        self.assertFalse(result["fixtures"][0]["certified"])
    def test_unverified_names_never_join_and_future_results_do_not_leak(self):
        future=datetime(2026,10,20,19,tzinfo=timezone.utc)
        h=[{"group":"I2|Serie B","date":future,"fixture_id":"x",
            "home":"api-team:136:11","away":"api-team:136:22",
            "hg":3,"ag":0}]
        fixture={"group":"I1|Serie A","fixtureId":"new",
            "leagueCode":"I1","date":datetime(2026,10,14,19,tzinfo=timezone.utc),
            "home":"A","away":"B","homeTeam":"A","awayTeam":"B"}
        v=cross_division_evidence(h,[fixture],datetime(2026,10,13,11,tzinfo=timezone.utc))
        self.assertEqual(v["fixtures"][0]["sides"]["home"]["otherCompetitionMatches"],0)
        self.assertEqual(v["counts"]["no_verified_other_division_history"],1)


if __name__=="__main__":
    unittest.main()
