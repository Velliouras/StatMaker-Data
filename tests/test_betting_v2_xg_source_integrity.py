"""Synthetic, offline V2 provider/cache integrity regressions."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from xg_source_integrity import audit_pair, audit_repository, observed_raw_xg, numeric_xg, safe_source


def fixture(fid, day, xg1, xg2):
    stats = []
    for name, xg in (("Home FC", xg1), ("Away FC", xg2)):
        fields = [{"type": "Shots on Goal", "value": 3}]
        if xg is not None:
            fields.append({"type": "expected_goals", "value": str(xg)})
        stats.append({"team": {"name": name}, "statistics": fields})
    return {"fixture_id": fid, "date": day, "status": "FT",
            "home_team": "Home FC", "away_team": "Away FC",
            "raw_statistics": stats,
            "normalized_stats": {"HxG": xg1, "AxG": xg2}}


class CacheXgTests(unittest.TestCase):
    def test_absent_raw_xg_is_not_synthesized(self):
        self.assertIsNone(observed_raw_xg(fixture(1, "2026-09-05T18:00:00Z", None, None)))
        self.assertIsNone(numeric_xg(None))
        self.assertIsNone(numeric_xg(float("nan")))
        self.assertIsNone(numeric_xg(True))

    def test_source_and_published_are_identical(self):
        src = fixture(1, "2026-09-05T18:00:00Z", 2.10, 0.80)
        published = {"matches": [{"fixture_id": 1, "date_utc": src["date"], "status": "FT",
                                  "home_team": src["home_team"], "away_team": src["away_team"],
                                  "normalized_stats": dict(src["normalized_stats"])}]}
        result = audit_pair(published, {"fixtures": [src]})["2026-09"]
        self.assertEqual(result["providerObservedBothXg"], 1)
        self.assertEqual(result["sourceVsPublishedXgMismatch"], 0)
        self.assertEqual(result["rawVsSourceNormalizedXgMismatch"], 0)

    def test_missing_source_xg_is_reported(self):
        src = fixture(1, "2026-09-05T18:00:00Z", None, None)
        published = {"matches": [{"fixture_id": 1, "date_utc": src["date"], "status": "FT",
                                  "home_team": "Home FC", "away_team": "Away FC",
                                  "normalized_stats": {"HxG": None, "AxG": None}}]}
        result = audit_pair(published, {"fixtures": [src]})["2026-09"]
        self.assertEqual(result["providerObservedBothXg"], 0)
        self.assertEqual(result["rawXgNotIndependentlyVerified"], 1)
        self.assertEqual(result["publishedBothXg"], 0)

    def test_mismatched_publication_is_detected(self):
        src = fixture(1, "2026-09-05T18:00:00Z", 2.10, 0.80)
        published = {"matches": [{"fixture_id": 1, "date_utc": src["date"], "status": "FT",
                                  "home_team": "Home FC", "away_team": "Away FC",
                                  "normalized_stats": {"HxG": 1.5, "AxG": 0.8}}]}
        result = audit_pair(published, {"fixtures": [src]})["2026-09"]
        self.assertEqual(result["sourceVsPublishedXgMismatch"], 1)

    def test_duplicate_provider_ids_fail_closed(self):
        src = fixture(1, "2026-09-05T18:00:00Z", 2.1, 0.8)
        published = {"matches": [{"fixture_id": 1, "date_utc": src["date"], "status": "FT",
                                  "home_team": "Home FC", "away_team": "Away FC",
                                  "normalized_stats": src["normalized_stats"]}]}
        result = audit_pair(published, {"fixtures": [src, src]})["2026-09"]
        self.assertEqual(result["invalidOrDuplicateFixtureIdentity"], 1)
        self.assertEqual(result["providerObservedBothXg"], 0)

    def test_team_order_not_guessed(self):
        src = fixture(1, "2026-09-05T18:00:00Z", 2.1, 0.8)
        src["raw_statistics"][0]["team"]["name"] = "Unknown Club"
        self.assertIsNone(observed_raw_xg(src))

    def test_report_can_run_on_restricted_local_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            idx = root / "data/statmaker/domestic_enriched/index.json"
            idx.parent.mkdir(parents=True)
            raw_path = root / "data/api_football/fixture_stats/test/2026/fixture_stats.json"
            raw_path.parent.mkdir(parents=True)
            enriched_path = idx.parent / "league_2026.json"
            src = fixture(1, "2026-09-05T18:00:00Z", 1.3, 1.1)
            raw_path.write_text(json.dumps({"fixtures": [src]}))
            enriched_path.write_text(json.dumps({"competition": {"league_code": "E0"},
                                                 "matches": [{**src, "date_utc": src["date"]}]}))
            idx.write_text(json.dumps({"schema_version": 3, "leagues": [{
                "league_code": "E0", "app_season": "2026-2027",
                "output_path": str(enriched_path.relative_to(root)),
                "cache_path": str(raw_path.relative_to(root))}]}))
            report = audit_repository(root, {"E0"})
            self.assertEqual(report["providerCalls"], 0)
            self.assertFalse(report["certified"])
            self.assertEqual(report["leagues"][0]["monthly"]["2026-09"]["publishedBothXg"], 1)
            with self.assertRaises(ValueError):
                safe_source(root, "../../secret.json", "data/statmaker/domestic_enriched/")


if __name__ == "__main__":
    unittest.main()
