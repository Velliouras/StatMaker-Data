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


if __name__ == "__main__":
    unittest.main()
