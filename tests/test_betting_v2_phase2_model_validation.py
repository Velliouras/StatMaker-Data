"""Fail-closed Phase-2 model isolation and pre-holdout calibration tests."""
from datetime import date,timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts/betting_v2"))
from phase2_model_validation import strict_evidence, frozen_market_calibration, validate_mode


class Phase2ChronologicalCalibrationTests(unittest.TestCase):
    @staticmethod
    def sample(day, fixture, *, p=.62, win=True, mode="ELO_GOALS_FALLBACK_NO_XG"):
        home=int(bool(win))
        probs={"1X2_HOME":p,"1X2_DRAW":.18,"1X2_AWAY":.82-p,
               "MATCH_OVER_2_5":p}
        return {"date":day.isoformat(),"fixtureId":fixture,
                "leagueCode":"E0","league":"E0|Premier League",
                "strategy":mode,"historicalXgSufficient":False,
                "probabilities":probs,
                "observed":{"1X2_HOME":home,"1X2_DRAW":0,
                            "1X2_AWAY":1-home,"MATCH_OVER_2_5":home}}
    def test_freeze_chronological_boundaries_without_leakage(self):
        cal=[self.sample(date(2025,5,1),str(i)) for i in range(40)]
        hold=[self.sample(date(2025,8,1),str(i+100)) for i in range(10)]
        result=validate_mode({},cal,hold,"ELO_GOALS_FALLBACK_NO_XG")
        self.assertEqual(result["status"],"RESEARCH_HOLDOUT_MEASURED")
        self.assertEqual(result["proof"]["calibrationFixtures"],40)
        self.assertEqual(result["proof"]["untouchedHoldoutFixtures"],10)
        self.assertFalse(result["canCertifyStrong"])
        self.assertEqual(result["publishedStrongPicks"],0)
    def test_reversed_calibration_holdout_is_blocked(self):
        cal=[self.sample(date(2026,9,2),"1")]
        hold=[self.sample(date(2026,9,1),"2")]
        self.assertEqual(strict_evidence(cal,hold,"ELO_GOALS_FALLBACK_NO_XG")["reason"],
                         "CHRONOLOGICAL_CALIBRATION_LEAKAGE")
    def test_fixture_overlap_is_blocked(self):
        cal=[self.sample(date(2025,5,1),"99")]
        hold=[self.sample(date(2025,8,1),"99")]
        self.assertEqual(strict_evidence(cal,hold,"ELO_GOALS_FALLBACK_NO_XG")["reason"],
                         "DUPLICATED_CALIBRATION_HOLDOUT_FIXTURE")
    def test_fallback_never_uses_xg_ready_cases(self):
        row=self.sample(date(2025,5,1),"x")
        row["historicalXgSufficient"]=True
        other=self.sample(date(2025,8,1),"z")
        proof=strict_evidence([row],[other],"ELO_GOALS_FALLBACK_NO_XG")
        self.assertFalse(proof["valid"])
        self.assertEqual(proof["reason"],"XG_PRIMARY_DATA_MIXED_INTO_FALLBACK")
    def test_missing_frozen_calibration_evidence_never_promotes_strong(self):
        cal=[self.sample(date(2025,5,1),str(i)) for i in range(4)]
        hold=[self.sample(date(2025,9,1),"future")]
        evidence=frozen_market_calibration(cal,hold)
        market=evidence["events"]["1X2_HOME"]
        self.assertEqual(market["holdout"]["n"],1)
        self.assertEqual(market["calibratedBinHoldout"]["n"],0)
        self.assertEqual(market["frozenConservativePAtLeast60Holdout"]["n"],0)
        self.assertFalse(market["canCertifyStrong"])
    def test_holdout_cannot_modify_frozen_calibration(self):
        cal=[self.sample(date(2025,5,1),str(i),win=i<34) for i in range(40)]
        hgood=[self.sample(date(2025,9,1),"g",win=True)]
        hbad=[self.sample(date(2025,9,1),"b",win=False)]
        good=frozen_market_calibration(cal,hgood)["events"]["1X2_HOME"]
        bad=frozen_market_calibration(cal,hbad)["events"]["1X2_HOME"]
        self.assertEqual(good["frozenCalibrationBinsByLeague"],bad["frozenCalibrationBinsByLeague"])
        self.assertNotEqual(good["holdout"]["observedHitRate"],bad["holdout"]["observedHitRate"])
    def test_league_cannot_borrow_other_leagues_calibration_samples(self):
        earlier=[self.sample(date(2025,5,1),str(i),win=True) for i in range(50)]
        # 50 prior examples exist, but none from the fixture's competition.
        out=self.sample(date(2025,9,1),"away_league")
        out["leagueCode"]="NOR2"
        out["league"]="NOR2|OBOS-ligaen"
        report=frozen_market_calibration(earlier,[out])
        case=report["events"]["1X2_HOME"]
        self.assertEqual(case["holdout"]["n"],1)
        self.assertEqual(case["calibratedBinHoldout"]["n"],0)
        self.assertEqual(case["frozenConservativePAtLeast60Holdout"]["n"],0)
        self.assertFalse(case["canCertifyStrong"])

    def test_over_under_use_exact_same_score_distribution(self):
        cal=[self.sample(date(2025,5,1),str(i),p=.7,win=i<30) for i in range(45)]
        hold=[self.sample(date(2025,9,1),"under_case",p=.7,win=False)]
        ev=frozen_market_calibration(cal,hold)
        over=ev["events"]["MATCH_OVER_2_5"]
        under=ev["events"]["MATCH_UNDER_2_5"]
        self.assertEqual(over["holdout"]["n"],1)
        self.assertEqual(under["holdout"]["n"],1)
        self.assertAlmostEqual(over["holdout"]["meanRawP"]+
                               under["holdout"]["meanRawP"],1,places=12)
        self.assertEqual(over["holdout"]["observedHitRate"]+
                         under["holdout"]["observedHitRate"],1)
        self.assertFalse(over["canCertifyStrong"])
        self.assertFalse(under["canCertifyStrong"])

    def test_invalid_distribution_blocks_research(self):
        cal=[self.sample(date(2025,5,1),"1")]
        hold=[self.sample(date(2025,9,1),"2")]
        hold[0]["probabilities"]["1X2_DRAW"]=.9
        with self.assertRaisesRegex(ValueError,"Invalid 1X2 distribution"):
            validate_mode({},cal,hold,"ELO_GOALS_FALLBACK_NO_XG")


if __name__=="__main__":
    unittest.main()
