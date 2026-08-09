import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_domestic_proposal_readiness as readiness


class DomesticProposalReadinessTest(unittest.TestCase):
    def setUp(self):
        self.matches = [
            {
                "home_team": "Alpha FC",
                "away_team": "Beta FC",
                "home_goals": 2,
                "away_goals": 1,
                "hthg": 1,
                "htag": 0,
                "normalized_stats": {
                    "HS": 12,
                    "AS": 8,
                    "HST": 5,
                    "AST": 3,
                    "HC": 7,
                    "AC": 4,
                    "HY": 3,
                    "AY": 2,
                    "HR": None,
                    "AR": None,
                },
            }
        ]

    def test_cards_require_yellow_cards_not_red_cards(self):
        support = readiness.historical_support(self.matches)
        row = readiness.market_support(support, "Alpha", "Beta", "MATCH_CARDS")
        self.assertTrue(row["hardHistoryValid"])
        self.assertEqual(1, row["homeSample"])
        self.assertEqual(1, row["awaySample"])

    def test_market_specific_readiness_does_not_require_unrelated_groups(self):
        match = dict(self.matches[0])
        match["normalized_stats"] = {"HC": 5, "AC": 6}
        support = readiness.historical_support([match])
        self.assertTrue(readiness.market_support(support, "Alpha", "Beta", "MATCH_CORNERS")["hardHistoryValid"])
        self.assertFalse(readiness.market_support(support, "Alpha", "Beta", "MATCH_SHOTS")["hardHistoryValid"])

    def test_asian_families_use_their_exact_historical_requirements(self):
        support = readiness.historical_support(self.matches)
        for market in ("ASIAN_HANDICAP", "ASIAN_GOALS"):
            self.assertTrue(readiness.market_support(support, "Alpha", "Beta", market)["hardHistoryValid"])
        for market in ("ASIAN_HANDICAP_1H", "ASIAN_GOALS_1H"):
            self.assertTrue(readiness.market_support(support, "Alpha", "Beta", market)["hardHistoryValid"])
        for market in ("ASIAN_CORNERS", "ASIAN_CORNER_HANDICAP"):
            self.assertTrue(readiness.market_support(support, "Alpha", "Beta", market)["hardHistoryValid"])

    def test_exact_visible_market_requires_bookmaker_exact_flag_and_minimum_odd(self):
        base = {"market": "MATCH_CORNERS", "bookmaker": "Bet365", "exactBookmakerOdds": True}
        self.assertTrue(readiness.valid_exact_market({**base, "odds": 1.20}))
        self.assertFalse(readiness.valid_exact_market({**base, "odds": 1.19}))
        self.assertFalse(readiness.valid_exact_market({**base, "odds": 1.50, "exactBookmakerOdds": False}))
        self.assertFalse(readiness.valid_exact_market({**base, "odds": 1.50, "bookmaker": ""}))

        asian = {"market": "ASIAN_GOALS", "bookmaker": "Bet365", "exactBookmakerOdds": True}
        self.assertTrue(readiness.valid_exact_market({**asian, "odds": 1.91}))

    def test_restored_non_core_league_can_be_proposal_ready(self):
        # CZE is intentionally outside the temporary core-27 odds polling scope,
        # but remains inside the 53-league Stats universe and may use preserved exact odds.
        index = {
            "leagues": [
                {
                    "league_code": "CZE",
                    "country": "Czech Republic",
                    "league": "Czech Liga",
                    "output_path": "does-not-exist-in-unit-test.json",
                }
            ]
        }
        scope = {"statsUniverseLeagueCodes": ["CZE"]}
        odds = {
            "leagues": [
                {
                    "leagueCode": "CZE",
                    "country": "Czech Republic",
                    "competition": "Czech Liga",
                    "matches": [],
                }
            ]
        }
        payload = readiness.build_payload(index, odds, scope)
        self.assertEqual("CZE", payload["leagues"][0]["leagueCode"])


if __name__ == "__main__":
    unittest.main()
