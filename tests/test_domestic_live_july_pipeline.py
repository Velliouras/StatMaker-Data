import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import domestic_live_july_pipeline as pipeline
import expand_domestic_full_stats as full_stats
import refresh_domestic_live_july_odds as odds_refresh


class DomesticLiveJulyPipelineTest(unittest.TestCase):
    def season(self, year, start, end):
        return {"year": year, "start": start, "end": end}

    def test_active_season_is_selected(self):
        today = dt.date(2026, 7, 10)
        result = pipeline.select_target_season(
            [self.season(2026, "2026-03-01", "2026-11-30")],
            today,
        )
        self.assertIsNotNone(result)
        season, lifecycle = result
        self.assertEqual(2026, season["year"])
        self.assertEqual("active", lifecycle)

    def test_every_july_start_date_is_selected(self):
        today = dt.date(2026, 7, 10)
        for day in range(1, 32):
            result = pipeline.select_target_season(
                [self.season(2026, f"2026-07-{day:02d}", "2027-05-31")],
                today,
            )
            self.assertIsNotNone(result, f"July {day} must be selected")

    def test_non_july_upcoming_season_is_not_selected(self):
        result = pipeline.select_target_season(
            [self.season(2026, "2026-08-01", "2027-05-31")],
            dt.date(2026, 7, 10),
        )
        self.assertIsNone(result)

    def test_upcoming_july_uses_previous_history_season(self):
        target = self.season(2026, "2026-07-25", "2027-05-30")
        previous = self.season(2025, "2025-07-20", "2026-05-30")
        chosen = pipeline.select_history_season(
            [previous, target],
            target,
            "starts_in_july",
        )
        self.assertEqual(2025, chosen["year"])

    def test_active_season_uses_current_history(self):
        target = self.season(2026, "2026-03-01", "2026-11-30")
        chosen = pipeline.select_history_season([target], target, "active")
        self.assertIs(target, chosen)

    def test_odds_merge_preserves_unprocessed_and_prunes_expired(self):
        registry = [
            {"leagueCode": "A", "country": "A", "competition": "A"},
            {"leagueCode": "B", "country": "B", "competition": "B"},
        ]
        previous = {
            "schemaVersion": 3,
            "leagues": [
                {
                    "leagueCode": "A",
                    "matches": [
                        {"date": "2026-07-09", "id": "expired"},
                        {"date": "2026-07-11", "id": "keep"},
                    ],
                },
                {"leagueCode": "B", "matches": [{"date": "2026-07-12", "id": "old-b"}]},
            ],
        }
        fresh = {
            "schemaVersion": 3,
            "generatedAt": "2026-07-10T12:00:00Z",
            "leagues": [
                {"leagueCode": "B", "matches": [{"date": "2026-07-13", "id": "new-b"}]},
            ],
        }
        merged = odds_refresh.safe_merge_odds_feed(previous, fresh, registry, dt.date(2026, 7, 10))
        by_code = {league["leagueCode"]: league for league in merged["leagues"]}
        self.assertEqual(["keep"], [match["id"] for match in by_code["A"]["matches"]])
        self.assertEqual(["new-b"], [match["id"] for match in by_code["B"]["matches"]])

    def test_empty_fresh_odds_do_not_erase_valid_previous_matches(self):
        registry = [{"leagueCode": "A", "country": "A", "competition": "A"}]
        previous = {
            "schemaVersion": 3,
            "leagues": [
                {"leagueCode": "A", "matches": [{"date": "2026-07-12", "id": "keep"}]},
            ],
        }
        fresh = {
            "schemaVersion": 3,
            "generatedAt": "2026-07-10T12:00:00Z",
            "leagues": [{"leagueCode": "A", "matches": []}],
        }
        merged = odds_refresh.safe_merge_odds_feed(previous, fresh, registry, dt.date(2026, 7, 10))
        self.assertEqual(["keep"], [match["id"] for match in merged["leagues"][0]["matches"]])
        self.assertEqual(["A"], merged["debug"]["preservedAfterEmptyRefresh"])

    def test_odds_validation_rejects_non_exact_market(self):
        registry = [{"leagueCode": "A"}]
        feed = {
            "leagues": [{
                "leagueCode": "A",
                "matches": [{
                    "date": "2026-07-12",
                    "teamMappingStatus": "matched",
                    "usableForStats": True,
                    "markets": [{"odds": 1.9, "bookmaker": "Bet365", "exactBookmakerOdds": False}],
                }],
            }],
        }
        with self.assertRaises(RuntimeError):
            odds_refresh.validate_feed(feed, registry, dt.date(2026, 7, 10))

    def test_odds_validation_accepts_exact_market(self):
        registry = [{"leagueCode": "A"}]
        feed = {
            "leagues": [{
                "leagueCode": "A",
                "matches": [{
                    "date": "2026-07-12",
                    "teamMappingStatus": "matched",
                    "usableForStats": True,
                    "markets": [{"odds": 1.9, "bookmaker": "Bet365", "exactBookmakerOdds": True}],
                }],
            }],
        }
        result = odds_refresh.validate_feed(feed, registry, dt.date(2026, 7, 10))
        self.assertEqual({"leagueCount": 1, "matchCount": 1, "marketCount": 1}, result)

    def test_app_season_label(self):
        self.assertEqual(
            "2026-2027",
            pipeline.app_season_label(dt.date(2026, 7, 25), dt.date(2027, 5, 30)),
        )
        self.assertEqual(
            "2026",
            pipeline.app_season_label(dt.date(2026, 3, 1), dt.date(2026, 11, 30)),
        )

    def test_full_stat_number_parser_handles_provider_percentages(self):
        self.assertEqual(89, full_stats.parse_number("89%"))
        self.assertEqual(4.72, full_stats.parse_number("4,72"))
        self.assertIsNone(full_stats.parse_number(""))
        self.assertIsNone(full_stats.parse_number(None))

    def test_full_stat_expansion_maps_home_and_away_values(self):
        fixture = {
            "home_team_id": 1,
            "away_team_id": 2,
            "home_team": "Home",
            "away_team": "Away",
            "normalized_stats": {},
            "raw_statistics": [
                {
                    "team": {"id": 1, "name": "Home"},
                    "statistics": [
                        {"type": "Shots off Goal", "value": 7},
                        {"type": "Passes %", "value": "84%"},
                    ],
                },
                {
                    "team": {"id": 2, "name": "Away"},
                    "statistics": [
                        {"type": "Shots off Goal", "value": 3},
                        {"type": "Passes %", "value": "76%"},
                    ],
                },
            ],
        }
        expanded, counts = full_stats.expand_fixture(fixture)
        stats = expanded["normalized_stats"]
        self.assertEqual(7, stats["HShotsOffGoal"])
        self.assertEqual(3, stats["AShotsOffGoal"])
        self.assertEqual(84, stats["HPassAccuracy"])
        self.assertEqual(76, stats["APassAccuracy"])
        self.assertEqual(1, counts["HShotsOffGoal"])
        self.assertEqual(1, counts["AShotsOffGoal"])


if __name__ == "__main__":
    unittest.main()
