import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import api_football_fetch_fixture_stats as stats_fetch
import refresh_domestic_live_july_stats as refresh


class DomesticStatsCompletenessTest(unittest.TestCase):
    def test_all_null_normalized_dictionary_is_incomplete(self):
        fixture = {
            "raw_statistics": [{"team": {"id": 1}, "statistics": []}],
            "normalized_stats": {"HS": None, "AS": None, "HC": None, "AC": None},
        }
        self.assertFalse(refresh.has_real_normalized_stats(fixture))
        self.assertFalse(stats_fetch.has_cached_stats(fixture))

    def test_one_real_provider_value_is_complete(self):
        fixture = {
            "raw_statistics": [{"team": {"id": 1}, "statistics": [{"type": "Total Shots", "value": 0}]}],
            "normalized_stats": {"HS": 0, "AS": None, "HC": None, "AC": None},
        }
        self.assertTrue(refresh.has_real_normalized_stats(fixture))
        self.assertTrue(stats_fetch.has_cached_stats(fixture))

    def test_normalized_value_without_raw_provider_response_is_incomplete(self):
        fixture = {
            "raw_statistics": [],
            "normalized_stats": {"HS": 5, "AS": 4},
        }
        self.assertFalse(refresh.has_real_normalized_stats(fixture))

    def test_final_score_does_not_make_missing_stats_complete(self):
        fixture = {
            "status": "FT",
            "home_goals": 2,
            "away_goals": 1,
            "raw_statistics": [],
            "normalized_stats": {"HS": None, "AS": None},
        }
        self.assertTrue(refresh.has_final_score(fixture))
        self.assertFalse(refresh.has_real_normalized_stats(fixture))


if __name__ == "__main__":
    unittest.main()
