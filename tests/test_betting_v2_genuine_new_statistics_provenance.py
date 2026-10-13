"""Offline regression for V2 source lineage: only genuinely received statistics.

The unit tests do not contact API-Football or mutate cached data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest

from scripts.betting_v2.production_contract import digest
from scripts.betting_v2.production_sources import capture_new_statistics


class GenuineV2StatisticsProvenanceTests(unittest.TestCase):
    def test_successful_provider_response_has_current_observation_and_real_hashes(self):
        item = {
            "fixture_id": 8123, "date": "2026-10-12T18:00:00+00:00",
            "home_team_id": 10, "away_team_id": 20,
            "home_team": "Home", "away_team": "Away",
            "home_goals": 2, "away_goals": 1, "status": "FT",
            "source_league": "E0",
            "normalized_stats": {"HxG": 1.2, "AxG": 0.8, "HC": 7, "AC": 2},
        }
        actual_response = [
            {"team": {"id": 10}, "statistics": [{"type": "expected_goals", "value": "1.2"}]},
            {"team": {"id": 20}, "statistics": [{"type": "expected_goals", "value": "0.8"}]},
        ]
        before = int(datetime.now(timezone.utc).timestamp() * 1000)
        receipt = capture_new_statistics(item, actual_response)
        after = int(datetime.now(timezone.utc).timestamp() * 1000)

        self.assertEqual(receipt["contract"], "statmaker-betting-v2-statistics-receipt-v1")
        self.assertEqual(receipt["provider"], "api-football")
        self.assertLessEqual(before, receipt["observedAtMs"])
        self.assertLessEqual(receipt["observedAtMs"], after)
        self.assertEqual(receipt["identitySha256"], digest(receipt["identity"]))
        self.assertEqual(receipt["rawStatisticsSha256"], digest(actual_response))
        self.assertEqual(receipt["normalizedSha256"], digest(item["normalized_stats"]))

    def test_default_capture_is_explicitly_opt_out_and_after_actual_provider_response(self):
        root = Path(__file__).resolve().parents[1]
        fetcher = (root / "scripts/api_football_fetch_fixture_stats.py").read_text(encoding="utf-8")
        normalized = 'merged["normalized_stats"] = normalize_statistics(raw_statistics, fixture)'
        guard = 'os.environ.get("STATMAKER_BETTING_V2_CAPTURE_LINEAGE", "1")'
        capture = 'capture_new_statistics(merged, raw_statistics)'
        self.assertIn(normalized, fetcher)
        self.assertIn(guard, fetcher)
        self.assertIn(capture, fetcher)
        self.assertLess(fetcher.index(normalized), fetcher.index(guard))
        self.assertLess(fetcher.index(guard), fetcher.index(capture))


if __name__ == "__main__":
    unittest.main()
