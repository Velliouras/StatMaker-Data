"""Zero-API regression suite: fixture attribution, UTC/Athens, exact ROI joins.

Uses synthetic temporary source files. It does not certify any betting model.
Run explicitly from repo root:
 python -m unittest tests/test_betting_v2_identity.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

MOD = Path(__file__).resolve().parents[1] / "scripts/betting_v2"
sys.path.insert(0, str(MOD))
from fixture_lookup import CachedFixtureLookup
from quote_join import exact_join, forecast_index


class BettingV2IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        index = root / "data/statmaker/domestic_enriched/index.json"
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({
            "schema_version": 3,
            "leagues": [{"output_path": "data/statmaker/domestic_enriched/england_2026.json"}]
        }), encoding="utf-8")
        (index.parent / "england_2026.json").write_text(json.dumps({
            "schema_version": 3,
            "competition": {"league_code": "E0"},
            "matches": [{
                "fixture_id": 987654,
                "date_utc": "2026-10-10T22:30:00+00:00",
                "home_team": "Manchester United", "away_team": "Leeds United"
            }]
        }), encoding="utf-8")
        self.resolver = CachedFixtureLookup(root)
        self.match = {
            "leagueCode": "E0", "homeTeam": "Manchester United",
            "awayTeam": "Leeds United"
        }
        self.kickoff = datetime(2026, 10, 10, 22, 30, tzinfo=timezone.utc)
        self.forecast = {
            "fixtureId": "987654", "leagueCode": "E0", "date": "2026-10-10",
            "kickoffUTC": "2026-10-10T22:30:00+00:00",
            "probabilities": {"1X2_HOME": .61, "1X2_DRAW": .23, "1X2_AWAY": .16},
            "homeGoals": 1, "awayGoals": 0
        }
        self.quote = {
            "date": "2026-10-11",  # Athens date, DIFFERENT from UTC forecast day
            "fixtureId": "987654", "leagueCode": "E0",
            "kickoffUTC": "2026-10-10T22:30:00+00:00",
            "quoteCutoff": "2026-10-11T00:00:00+03:00",
            "identityResolution": "EXACT_CACHED_FIXTURE",
            "selectionKey": "fixture987654-1X2-home",
            "market": "RESULT_1X2", "direction": "HOME", "odd": 2.0,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_idless_price_matches_unique_cached_fixture(self):
        fixture, reason = self.resolver.resolve(self.match, self.kickoff, None)
        self.assertEqual((fixture, reason), ("987654", "EXACT_CACHED_FIXTURE"))

    def test_wrong_provider_id_or_team_is_rejected(self):
        fixture, reason = self.resolver.resolve(self.match, self.kickoff, "999999")
        self.assertIsNone(fixture)
        self.assertEqual(reason, "UNVERIFIED_EXPLICIT_FIXTURE_ID")
        fixture, reason = self.resolver.resolve(
            {**self.match, "awayTeam": "Birmingham"}, self.kickoff, None
        )
        self.assertEqual((fixture, reason), (None, "NO_EXACT_KICKOFF_TEAM_MATCH"))

    def test_wrong_kickoff_rejected(self):
        late = datetime(2026, 10, 11, 0, 15, tzinfo=timezone.utc)
        fixture, reason = self.resolver.resolve(self.match, late, None)
        self.assertEqual((fixture, reason), (None, "NO_EXACT_KICKOFF_TEAM_MATCH"))

    def test_attribution_rejects_different_league(self):
        forecast, status = exact_join({**self.quote, "leagueCode": "D1"},
                                      forecast_index([self.forecast]))
        self.assertIsNone(forecast)
        self.assertEqual(status, "NO_UNTOUCHED_FORECAST_FOR_IDENTICAL_LEAGUE_FIXTURE")

    def test_midnight_crossing_does_not_erase_quote(self):
        joined, status = exact_join(self.quote, forecast_index([self.forecast]))
        self.assertEqual(status, "EXACT_PREMATCH_JOIN")
        self.assertEqual(joined, self.forecast)

    def test_after_kickoff_quote_cannot_enter_roi(self):
        joined, status = exact_join(
            {**self.quote, "quoteCutoff": "2026-10-11T02:00:00+03:00"},
            forecast_index([self.forecast]))
        self.assertIsNone(joined)
        self.assertEqual(status, "PRICE_NOT_PRE_KICKOFF")

    def test_unverified_quote_metadata_fails_closed(self):
        joined, status = exact_join(
            {**self.quote, "identityResolution": "NOT_CONFIRMED"},
            forecast_index([self.forecast]))
        self.assertIsNone(joined)
        self.assertEqual(status, "UNVERIFIED_PRICE_FIXTURE_IDENTITY")

    def test_duplicate_holdout_fixture_is_fatal(self):
        with self.assertRaises(ValueError):
            forecast_index([self.forecast, dict(self.forecast)])


if __name__ == "__main__":
    unittest.main()
