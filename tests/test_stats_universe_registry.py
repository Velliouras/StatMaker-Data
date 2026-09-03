import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_scoped_domestic_registry as registry


class StatsUniverseRegistryTest(unittest.TestCase):
    @staticmethod
    def season(year, start, end):
        return {"year": year, "start": start, "end": end}

    def test_active_season_is_selected(self):
        result = registry.select_target_season_rolling(
            [self.season(2026, "2026-03-01", "2026-11-30")],
            dt.date(2026, 7, 24),
        )
        self.assertIsNotNone(result)
        self.assertEqual("active", result[1])

    def test_august_first_is_selected_from_july_twenty_four(self):
        result = registry.select_target_season_rolling(
            [self.season(2026, "2026-08-01", "2027-05-31")],
            dt.date(2026, 7, 24),
        )
        self.assertIsNotNone(result)
        self.assertEqual("starts_soon", result[1])

    def test_start_inside_rolling_45_day_horizon_is_selected(self):
        result = registry.select_target_season_rolling(
            [self.season(2026, "2026-09-01", "2027-05-31")],
            dt.date(2026, 7, 24),
        )
        self.assertIsNotNone(result)
        self.assertEqual("starts_soon", result[1])

    def test_start_outside_rolling_45_day_horizon_is_not_selected(self):
        result = registry.select_target_season_rolling(
            [self.season(2026, "2026-09-15", "2027-05-31")],
            dt.date(2026, 7, 24),
        )
        self.assertIsNone(result)

    def test_current_major_league_rollovers_keep_2025_history(self):
        cases = {
            "E0": ("2026-08-21", "2027-05-30"),
            "F1": ("2026-08-21", "2027-05-29"),
            "I1": ("2026-08-22", "2027-05-30"),
            "SP1": ("2026-08-14", "2027-05-23"),
            "G1": ("2026-08-22", "2027-03-20"),
        }
        for code, (target_start, target_end) in cases.items():
            with self.subTest(league_code=code):
                start = dt.date.fromisoformat(target_start)
                end = dt.date.fromisoformat(target_end)
                previous = self.season(
                    2025,
                    start.replace(year=2025).isoformat(),
                    end.replace(year=2026).isoformat(),
                )
                target = self.season(2026, target_start, target_end)
                chosen = registry.select_history_season_rolling(
                    [previous, target],
                    target,
                    "active",
                    dt.date(2026, 8, 22),
                )
                self.assertIs(previous, chosen)

    def test_calendar_year_active_season_keeps_current_history(self):
        previous = self.season(2025, "2025-03-15", "2025-11-30")
        target = self.season(2026, "2026-03-14", "2026-12-06")
        chosen = registry.select_history_season_rolling(
            [previous, target],
            target,
            "active",
            dt.date(2026, 3, 20),
        )
        self.assertIs(target, chosen)

    def test_mature_cross_year_active_season_uses_current_history(self):
        previous = self.season(2025, "2025-08-15", "2026-05-24")
        target = self.season(2026, "2026-08-14", "2027-05-23")
        chosen = registry.select_history_season_rolling(
            [previous, target],
            target,
            "active",
            dt.date(2026, 10, 24),
        )
        self.assertIs(target, chosen)

    def test_upcoming_cross_year_season_uses_previous_history(self):
        previous = self.season(2025, "2025-08-15", "2026-05-24")
        target = self.season(2026, "2026-08-14", "2027-05-23")
        chosen = registry.select_history_season_rolling(
            [previous, target],
            target,
            "starts_soon",
            dt.date(2026, 8, 1),
        )
        self.assertIs(previous, chosen)

    def test_runtime_history_patch_does_not_recurse_for_upcoming(self):
        previous = self.season(2025, "2025-08-15", "2026-05-24")
        target = self.season(2026, "2026-08-14", "2027-05-23")
        original_selector = registry.pipeline.select_history_season
        registry.pipeline.select_history_season = lambda seasons, selected, lifecycle: registry.select_history_season_rolling(
            seasons,
            selected,
            lifecycle,
            dt.date(2026, 8, 1),
        )
        try:
            chosen = registry.pipeline.select_history_season(
                [previous, target],
                target,
                "starts_soon",
            )
        finally:
            registry.pipeline.select_history_season = original_selector
        self.assertIs(previous, chosen)

    def test_published_index_retains_completed_configured_league_outside_rolling_registry(self):
        refreshed = [
            {"league_code": "FIN2", "app_season": "2026"},
        ]
        previous = [
            {"league_code": "FIN", "app_season": "2026", "output_path": ""},
            {"league_code": "FIN2", "app_season": "2026", "output_path": ""},
            {"league_code": "G2", "app_season": "2025-2026", "output_path": ""},
            {"league_code": "OUTSIDE_SCOPE", "app_season": "2026", "output_path": ""},
        ]
        retained = registry.pipeline._published_stats_continuity_rows(refreshed, previous)
        self.assertEqual(["FIN", "G2"], [row["league_code"] for row in retained])


if __name__ == "__main__":
    unittest.main()
