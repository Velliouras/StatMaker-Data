import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rebuild_domestic_corners_from_archive as corners
import update_domestic_odds_api_io_push_aware as market_contract


class CanonicalMarketContractTest(unittest.TestCase):
    def test_only_exact_full_time_corner_totals_are_allowed(self):
        self.assertTrue(corners.is_supported_full_time_corner_market("Corners Totals"))
        self.assertTrue(corners.is_supported_full_time_corner_market("Corners Totals Home"))
        self.assertTrue(corners.is_supported_full_time_corner_market("Corners Totals Away"))
        for raw_name in (
            "Total Corners",
            "Alternative Corners",
            "Corners Totals HT",
            "Corners Race",
            "Corners Spread",
            "Corner Handicap",
            "First Match Corner",
            "Time of First Corner",
        ):
            self.assertFalse(
                corners.is_supported_full_time_corner_market(raw_name),
                raw_name,
            )

    def test_corner_totals_require_structured_line_and_complete_pair(self):
        market = {
            "name": "Corners Totals",
            "odds": [
                {"hdp": 10.5, "over": "1.91", "under": "1.91"},
                {"label": "Over 11.5", "over": "2.10", "under": "1.70"},
                {"hdp": 12.5, "over": "1.95"},
            ],
        }
        self.assertEqual(
            [{"hdp": 10.5, "over": "1.91", "under": "1.91"}],
            corners.explicit_corner_ou_rows(market),
        )

    def test_categorical_total_corners_cannot_become_match_corners(self):
        match = self._archive_match(
            "Total Corners",
            [
                {"label": "Under 6", "odds": "10.000"},
                {"label": "6 - 8", "odds": "3.200"},
                {"label": "9 - 11", "odds": "2.600"},
                {"label": "Over 14", "odds": "10.000"},
            ],
        )
        self.assertEqual([], corners.normalize_archived_corners(match))

    def test_alternative_corners_cannot_become_match_corners(self):
        match = self._archive_match(
            "Alternative Corners",
            [
                {"hdp": 2, "over": "1.02", "under": "41.0"},
                {"hdp": 24.5, "over": "1.909", "under": "1.80"},
            ],
        )
        self.assertEqual([], corners.normalize_archived_corners(match))

    def test_exact_corner_totals_still_publish(self):
        match = self._archive_match(
            "Corners Totals",
            [{"hdp": 10, "over": "1.91", "under": "1.91"}],
        )
        rows = corners.normalize_archived_corners(match)
        self.assertEqual(
            {
                ("MATCH_CORNERS", "Corners Over 10", 10.0, 1.91),
                ("MATCH_CORNERS", "Corners Under 10", 10.0, 1.91),
            },
            {
                (row["market"], row["selection"], row["line"], row["odds"])
                for row in rows
            },
        )

    def test_team_market_label_keeps_metric_identity(self):
        rows = market_contract._explicit_team_market_labels(
            [
                {
                    "market": "TEAM_SHOTS_ON_TARGET",
                    "selection": "Borussia Dortmund Under 5.5",
                    "odds": 1.833,
                    "line": 5.5,
                    "team": "Borussia Dortmund",
                },
                {
                    "market": "TEAM_CORNERS",
                    "selection": "Borussia Dortmund Over 5.5",
                    "odds": 1.80,
                    "line": 5.5,
                    "team": "Borussia Dortmund",
                },
            ]
        )
        self.assertEqual(
            "Borussia Dortmund Shots on Target Under 5.5",
            rows[0]["selection"],
        )
        self.assertEqual(
            "Borussia Dortmund Corners Over 5.5",
            rows[1]["selection"],
        )

    @staticmethod
    def _archive_match(raw_name, odds_rows):
        return {
            "id": "fixture-1",
            "homeTeam": "Home",
            "awayTeam": "Away",
            "teamMappingStatus": "matched",
            "providerMarkets": [
                {
                    "bookmaker": "Bet365",
                    "exactProviderPayload": True,
                    "market": {"name": raw_name, "odds": odds_rows},
                }
            ],
        }


if __name__ == "__main__":
    unittest.main()
