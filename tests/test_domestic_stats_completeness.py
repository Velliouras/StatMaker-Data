import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import api_football_fetch_fixture_stats as stats_fetch
import refresh_domestic_live_july_stats as refresh
import run_domestic_stats_refresh as orchestrator


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

    def test_fetch_refuses_wrong_provider_competition_before_cache_write(self):
        league = {
            "leagueCode": "FIN2",
            "country": "Finland",
            "competition": "Ykkösliiga",
            "display_name": "Ykkösliiga",
            "apiFootballLeagueId": 245,
            "api_football_league_id": 245,
            "season": "2026",
        }
        wrong_fixture = {
            "league": {
                "id": 245,
                "name": "Ykkönen",
                "country": "Finland",
                "season": 2026,
            },
            "fixture": {
                "id": 1,
                "date": "2026-08-29T12:00:00+00:00",
                "status": {"short": "NS"},
            },
            "teams": {
                "home": {"id": 1, "name": "Wrong A"},
                "away": {"id": 2, "name": "Wrong B"},
            },
        }
        request_state = {"count": 1}

        with (
            mock.patch.object(refresh.stats_fetch, "cache_path_for", return_value=ROOT / "fake-cache.json"),
            mock.patch.object(refresh.stats_fetch, "load_json", return_value={}),
            mock.patch.object(
                refresh.stats_fetch,
                "fetch_fixtures_with_fallback",
                return_value=([wrong_fixture], [], "league+season:2026", []),
            ),
            mock.patch.object(refresh.stats_fetch, "write_json") as write_json,
        ):
            row = refresh.fetch_league_preserving_fixture_metadata(
                "key", league, request_state, 10
            )

        self.assertEqual(0, row["completed_fixtures_found"])
        self.assertIn("provider competition mismatch", row["notes"])
        write_json.assert_called_once()
        written_payload = write_json.call_args.args[1]
        self.assertEqual([], written_payload["fixtures"])

    def test_wrong_provider_competition_name_invalidates_cache(self):
        league = {
            "apiFootballLeagueId": 245,
            "competition": "Ykkösliiga",
            "display_name": "Ykkösliiga",
            "season": "2026",
        }
        cache = {
            "league_id": 245,
            "season": "2026",
            "fixtures": [{
                "source_league": {
                    "id": 245,
                    "name": "Ykkönen",
                    "season": 2026,
                }
            }],
        }

        reason = stats_fetch.cache_identity_mismatch_reason(league, cache)

        self.assertIsNotNone(reason)
        self.assertIn("Ykkönen", reason)
        self.assertIn("Ykkösliiga", reason)

    def test_verified_provider_competition_aliases_are_compatible(self):
        self.assertTrue(
            stats_fetch.provider_league_name_compatible(
                "OBOS-ligaen",
                "1. Division",
            )
        )
        self.assertTrue(
            stats_fetch.provider_league_name_compatible(
                "Liga Portugal",
                "Primeira Liga",
            )
        )

    def test_provider_competition_token_order_difference_is_compatible(self):
        self.assertTrue(
            stats_fetch.provider_league_name_compatible(
                "Bundesliga 2",
                "2. Bundesliga",
            )
        )




class DomesticRosterDiscoveryTest(unittest.TestCase):
    def test_same_season_league_is_not_excluded_from_roster_discovery(self):
        league = {
            "leagueCode": "USA",
            "country": "USA",
            "competition": "Major League Soccer",
            "display_name": "Major League Soccer",
            "apiFootballLeagueId": 253,
            "season": "2026",
            "historyApiSeason": "2026",
            "targetApiSeason": "2026",
            "targetAppSeason": "2026",
        }
        fixture_payload = {
            "response": [
                {
                    "teams": {
                        "home": {"name": "Inter Miami"},
                        "away": {"name": "FC Dallas"},
                    }
                }
            ]
        }
        written = {}

        def fake_api_get(_key, _endpoint, _params, request_state, _max_requests):
            request_state["count"] += 1
            return fixture_payload

        with (
            mock.patch.object(orchestrator, "load_json", return_value={}),
            mock.patch.object(orchestrator, "write_json", side_effect=lambda path, payload: written.update({"payload": payload})),
            mock.patch.object(orchestrator, "cached_target_roster", return_value=[]),
            mock.patch.object(orchestrator.target.stats_fetch, "api_get", side_effect=fake_api_get) as api_get,
        ):
            used = orchestrator.discover_missing_target_rosters("key", [league], 20)

        self.assertEqual(1, used)
        api_get.assert_called_once()
        self.assertEqual(1, written["payload"]["leagueCount"])
        self.assertEqual("USA", written["payload"]["leagues"][0]["leagueCode"])
        self.assertEqual(["FC Dallas", "Inter Miami"], written["payload"]["leagues"][0]["teams"])

    def test_cached_target_fixture_roster_costs_zero_provider_calls(self):
        league = {
            "leagueCode": "FIN",
            "country": "Finland",
            "competition": "Veikkausliiga",
            "display_name": "Veikkausliiga",
            "apiFootballLeagueId": 244,
            "season": "2026",
            "targetApiSeason": "2026",
            "targetAppSeason": "2026",
        }
        written = {}

        with (
            mock.patch.object(orchestrator, "load_json", return_value={}),
            mock.patch.object(orchestrator, "write_json", side_effect=lambda path, payload: written.update({"payload": payload})),
            mock.patch.object(orchestrator, "cached_target_roster", return_value=["HJK helsinki", "Inter Turku"]),
            mock.patch.object(orchestrator.target.stats_fetch, "api_get") as api_get,
        ):
            used = orchestrator.discover_missing_target_rosters("key", [league], 20)

        self.assertEqual(0, used)
        api_get.assert_not_called()
        self.assertEqual(1, written["payload"]["leagueCount"])

    def test_normal_run_uses_full_registry_but_targeted_run_stays_targeted(self):
        registry = [{"leagueCode": "A"}, {"leagueCode": "B"}]
        selected = [{"leagueCode": "A"}]

        self.assertIs(
            registry,
            orchestrator.roster_discovery_scope(registry, selected, set(), False),
        )
        self.assertIs(
            selected,
            orchestrator.roster_discovery_scope(registry, selected, {"A"}, False),
        )


if __name__ == "__main__":
    unittest.main()
