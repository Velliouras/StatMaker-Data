"""Use existing API-Football scheduled fixtures, not odds-derived IDs or fuzzy aliases."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/betting_v2"))
from future_fixture_crosswalk import CachedUpcomingFixtureIndex


def test_cache(root, *, schedule=None, season="2026", league_id=71):
    root = Path(root)
    index = root / "data/statmaker/domestic_enriched/index.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({
        "schema_version": 3,
        "leagues": [{
            "league_code": "BRA",
            "api_football_league_id": 71,
            "api_football_season": "2026",
            "cache_path": "data/api_football/bra/fixture_stats.json",
        }],
    }))
    cache = root / "data/api_football/bra/fixture_stats.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({
        "provider": "api-football",
        "league_id": league_id,
        "season": season,
        "schedule_fixtures": schedule if schedule is not None else [
            {
                "fixture_id": 987654,
                "date": "2026-10-11T20:30:00Z",
                "home_team": "Palmeiras",
                "away_team": "Flamengo",
                "status": "NS",
                "fixture_query_used": "league+season:2026",
                "source_league": {"id": 71, "season": 2026},
            },
        ],
    }))


MATCH = {
    "leagueCode": "BRA", "home": "Palmeiras", "away": "Flamengo",
    "kickoffUTC": "2026-10-11T20:30:00+00:00",
}


class FutureApiFootballCrosswalkTests(unittest.TestCase):
    def test_exact_independent_fixture_id_in_cached_schedule(self):
        with tempfile.TemporaryDirectory() as t:
            test_cache(t)
            index = CachedUpcomingFixtureIndex(Path(t))
            row, reason = index.resolve(MATCH)
            self.assertEqual(reason, "EXACT_INDEPENDENT_API_FOOTBALL_FUTURE_FIXTURE")
            self.assertEqual(row["apiFootballFixtureId"], "987654")
            self.assertEqual(row["apiFootballLeagueId"], 71)
            self.assertEqual(index.entries, 1)

    def test_different_teams_or_kickoff_do_not_resolve(self):
        with tempfile.TemporaryDirectory() as t:
            test_cache(t)
            index = CachedUpcomingFixtureIndex(Path(t))
            for changed in ({"home": "Palmeiras B"},
                            {"kickoffUTC": "2026-10-11T21:00:00Z"},
                            {"leagueCode": "ARG"}):
                row, _ = index.resolve({**MATCH, **changed})
                self.assertIsNone(row)

    def test_uncertain_match_status_and_fallback_season_are_rejected(self):
        base = {
            "fixture_id": 99,
            "date": "2026-10-11T20:30:00Z",
            "home_team": "Palmeiras", "away_team": "Flamengo",
            "status": "NS",
            "source_league": {"id": 71, "season": 2026},
            "fixture_query_used": "league+season:2026",
        }
        for override in ({"status": "FT"},
                         {"status": "PST"},
                         {"fixture_query_used": "league+season:2025"},
                         {"source_league": {"id": 72, "season": 2026}},
                         {"source_league": {"id": 71, "season": 2025}}):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as t:
                test_cache(t, schedule=[{**base, **override}])
                index = CachedUpcomingFixtureIndex(Path(t))
                self.assertIsNone(index.resolve(MATCH)[0])

    def test_wrong_cache_league_and_season_rejected(self):
        for arguments in ({"league_id": 72}, {"season": "2025"}):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as t:
                test_cache(t, **arguments)
                index = CachedUpcomingFixtureIndex(Path(t))
                self.assertIsNone(index.resolve(MATCH)[0])

    def test_collision_of_two_different_fixture_ids_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as t:
            test_cache(t)
            path = Path(t) / "data/api_football/bra/fixture_stats.json"
            doc = json.loads(path.read_text())
            doc["schedule_fixtures"].append({
                **doc["schedule_fixtures"][0], "fixture_id": 987655,
            })
            path.write_text(json.dumps(doc))
            index = CachedUpcomingFixtureIndex(Path(t))
            self.assertEqual(index.resolve(MATCH)[1],
                             "AMBIGUOUS_API_FOOTBALL_FIXTURE")


if __name__ == "__main__":
    unittest.main()
