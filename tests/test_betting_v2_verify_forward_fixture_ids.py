from __future__ import annotations
import gzip
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from verify_forward_fixture_ids import audit
from future_fixture_crosswalk import CachedUpcomingFixtureIndex

NOW = datetime(2026, 10, 10, 22, tzinfo=timezone.utc)
ASOF = "2026-10-10T21:00:00+00:00"

class FixtureIdArchiveTests(unittest.TestCase):
    def test_independent_cache_identity_and_missing_scores(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "data/api_football/schedule.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({
                "provider": "api-football", "league_id": 71, "season": "2026",
                "schedule_fixtures": [{
                    "fixture_id": 987, "home_team": "A FC", "away_team": "B FC",
                    "date": "2026-10-11T18:00:00Z", "status": "NS",
                    "fixture_query_used": "league+season:2026",
                    "source_league": {"id": 71, "season": 2026}
                }]
            }))
            ix = root / "data/statmaker/domestic_enriched/index.json"
            ix.parent.mkdir(parents=True)
            ix.write_text(json.dumps({
                "schema_version": 3, "leagues": [{
                    "league_code": "BRA", "api_football_league_id": 71,
                    "api_football_season": "2026",
                    "cache_path": "data/api_football/schedule.json"
                }]
            }))
            folder = root / "reports/betting_v2/forward_snapshots"
            folder.mkdir(parents=True)
            row = {
                "contract": "statmaker-v2-immutable-shadow-forecast-v1",
                "providerEventId": "bookie123", "leagueCode": "BRA",
                "providerHomeTeam": "A FC", "providerAwayTeam": "B FC",
                "kickoffUTC": "2026-10-11T18:00:00Z",
                "forecastComputedAtUTC": ASOF,
                "strategy": "ELO_GOALS_FALLBACK_NO_XG",
                "noObservedTargetScoreUsed": True, "certifiedStrong": False,
                "researchOnly": True
            }
            envelope = {
                "contract": "statmaker-v2-immutable-shadow-snapshot-set-v1",
                "certificationStatus": "BLOCKED",
                "realStrongSelections": 0,
                "forecastComputedAtUTC": ASOF,
                "forecastCount": 1, "forecastData": [row],
                "historicalFeatureArchiveSha256": "a"*64,
                "providerScheduleSnapshotSha256": "b"*64
            }
            with gzip.open(folder / "first.json.gz", "wt", encoding="utf-8") as handle:
                json.dump(envelope, handle)
            result = audit(root, NOW)
            self.assertEqual(result["independentFixtureCrosswalksVerified"], 1)
            self.assertEqual(result["verifiedFixtureLinks"][0]["apiFootballFixtureId"], "987")
            self.assertIsNone(result["certifiedROI"])
            row["homeGoals"] = 2
            with gzip.open(folder / "first.json.gz", "wt", encoding="utf-8") as handle:
                json.dump(envelope, handle)
            invalid = audit(root, NOW)
            self.assertEqual(invalid["independentFixtureCrosswalksVerified"], 0)

if __name__ == "__main__":
    unittest.main()
