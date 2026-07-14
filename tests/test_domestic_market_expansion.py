import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import domestic_live_july_pipeline as pipeline
import domestic_odds_expansion as expansion
import odds_api_io_market_audit as audit
import rebuild_domestic_corners_from_archive as corner_rebuild
import refresh_domestic_live_july_odds as refresh
import update_domestic_odds_api_io as odds

expansion.install(odds, pipeline)


class DomesticMarketExpansionTest(unittest.TestCase):
    def test_double_chance_is_supported(self):
        result = audit.classify_provider_market("Double Chance")
        self.assertEqual("DOUBLE_CHANCE", result["family"])
        self.assertEqual("supported", result["status"])

    def test_unibet_double_chance_shape(self):
        debug = {}
        rows = odds.normalize_market(
            {
                "name": "Double Chance",
                "odds": [{"1X": "1.24", "12": "1.23", "X2": "1.80"}],
            },
            "Unibet",
            "VPS",
            "SJK",
            debug,
        )
        by_selection = {row["selection"]: row for row in rows}
        self.assertEqual({"1X", "12", "X2"}, set(by_selection))
        self.assertEqual(1.24, by_selection["1X"]["odds"])
        self.assertEqual(1.23, by_selection["12"]["odds"])
        self.assertEqual(1.80, by_selection["X2"]["odds"])
        self.assertTrue(all(row["exactBookmakerOdds"] for row in rows))

    def test_bet365_label_double_chance_shape(self):
        debug = {}
        rows = odds.normalize_market(
            {
                "name": "Double Chance",
                "odds": [
                    {"label": "VPS Vaasa or Draw", "under": "1.363"},
                    {"label": "Draw or SJK", "under": "1.727"},
                    {"label": "VPS Vaasa or SJK", "under": "1.285"},
                ],
            },
            "Bet365",
            "VPS",
            "SJK",
            debug,
        )
        self.assertEqual(
            {"1X": 1.363, "X2": 1.727, "12": 1.285},
            {row["selection"]: row["odds"] for row in rows},
        )

    def test_club_prefix_simplification(self):
        self.assertEqual("necaxa", expansion.simplified_team_name(odds, "Club Necaxa"))
        self.assertEqual("fcsb", expansion.simplified_team_name(odds, "Fotbal Club FCSB"))
        self.assertEqual("toluca", expansion.simplified_team_name(odds, "Deportivo Toluca FC"))
        self.assertEqual("rapid bucuresti", expansion.simplified_team_name(odds, "Rapid Bucuresti 1923"))

    def test_exact_alias_is_preferred(self):
        debug = {}
        mapped, canonical = odds.canonical_team_info(
            "Club Necaxa",
            "MEX",
            {"MEX": {"necaxa": "Necaxa"}},
            debug,
        )
        self.assertEqual("Necaxa", mapped)
        self.assertEqual("Necaxa", canonical)
        self.assertFalse(debug.get("unmatchedTeams"))

    def test_rotating_refresh_rebuilds_real_integer_corners_after_league_replacement(self):
        registry = [
            {
                "leagueCode": "AUT",
                "country": "Austria",
                "competition": "Bundesliga",
                "targetAppSeason": "2026-2027",
                "apiFootballLeagueId": 218,
                "enabledForOdds": True,
                "enabledForBetting": True,
            }
        ]
        previous = {
            "schemaVersion": 3,
            "leagues": [
                {
                    "leagueCode": "AUT",
                    "matches": [
                        self._canonical_match(
                            markets=[
                                self._market("MATCH_CORNERS", "Corners Over 10", 2.20, line=10.0)
                            ]
                        )
                    ],
                }
            ],
        }
        fresh = {
            "schemaVersion": 3,
            "generatedAt": "2026-07-14T15:03:00Z",
            "leagues": [
                {
                    "leagueCode": "AUT",
                    "matches": [self._canonical_match(markets=[self._market("1X2", "Home", 1.80)])],
                }
            ],
        }
        previous_archive = {"schemaVersion": 1, "leagues": []}
        fresh_archive = {
            "schemaVersion": 1,
            "generatedAt": "2026-07-14T15:03:00Z",
            "leagues": [
                {
                    "leagueCode": "AUT",
                    "matches": [
                        {
                            "id": "fixture-1",
                            "date": "2026-07-31",
                            "homeTeam": "Lask Linz",
                            "awayTeam": "Grazer AK",
                            "teamMappingStatus": "matched",
                            "providerMarkets": [
                                {
                                    "bookmaker": "Bet365",
                                    "exactProviderPayload": True,
                                    "market": {
                                        "name": "Corners Totals",
                                        "odds": [{"hdp": 10, "over": "1.91", "under": "1.91"}],
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        feed, archive, report = refresh.merge_refresh_payloads(
            previous,
            fresh,
            previous_archive,
            fresh_archive,
            registry,
            dt.date(2026, 7, 14),
        )

        markets = feed["leagues"][0]["matches"][0]["markets"]
        corners = [row for row in markets if row["market"] == "MATCH_CORNERS"]
        self.assertEqual(
            {
                ("Corners Over 10", 10.0, 1.91, "Bet365"),
                ("Corners Under 10", 10.0, 1.91, "Bet365"),
            },
            {
                (row["selection"], row["line"], row["odds"], row["bookmaker"])
                for row in corners
            },
        )
        self.assertTrue(all(row["exactBookmakerOdds"] is True for row in corners))
        self.assertEqual(2, report["totalCanonicalCornerSelections"])
        self.assertFalse(report["syntheticOdds"])
        self.assertEqual(1, report["canonicalFixturesMatched"])
        self.assertEqual("AUT", archive["leagues"][0]["leagueCode"])
        debug_report = feed["debug"]["cornerArchiveRebuild"]
        self.assertEqual(2, debug_report["totalCanonicalCornerSelections"])
        self.assertEqual(1, debug_report["canonicalFixturesMatched"])
        self.assertFalse(debug_report["syntheticOdds"])

    def test_missing_corner_payload_does_not_create_synthetic_fallback(self):
        feed = {
            "leagues": [
                {
                    "leagueCode": "AUT",
                    "matches": [self._canonical_match(markets=[self._market("1X2", "Home", 1.80)])],
                }
            ]
        }
        archive = {"leagues": [{"leagueCode": "AUT", "matches": []}]}

        report = corner_rebuild.rebuild_feed_corners(feed, archive, require_corners=False)

        markets = feed["leagues"][0]["matches"][0]["markets"]
        self.assertFalse(any(row["market"] in corner_rebuild.CORNER_MARKETS for row in markets))
        self.assertEqual(0, report["totalCanonicalCornerSelections"])
        self.assertFalse(report["syntheticOdds"])

    @staticmethod
    def _market(market, selection, price, line=None):
        row = {
            "market": market,
            "selection": selection,
            "odds": price,
            "bookmaker": "Bet365",
            "confidence": "high",
            "exactBookmakerOdds": True,
        }
        if line is not None:
            row["line"] = line
        return row

    @staticmethod
    def _canonical_match(markets):
        return {
            "id": "fixture-1",
            "date": "2026-07-31",
            "kickoff": "2026-07-31T17:30:00Z",
            "homeTeam": "Lask Linz",
            "awayTeam": "Grazer AK",
            "canonicalHomeTeam": "Lask Linz",
            "canonicalAwayTeam": "Grazer AK",
            "teamMappingStatus": "matched",
            "usableForStats": True,
            "markets": markets,
        }


if __name__ == "__main__":
    unittest.main()
