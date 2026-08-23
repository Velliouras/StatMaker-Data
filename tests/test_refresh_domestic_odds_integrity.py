import copy
import datetime as dt
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "refresh_domestic_odds_integrity.py"
UTC = dt.timezone.utc


def load_subject():
    target = types.ModuleType("refresh_domestic_live_july_odds")
    target.odds_fetch = types.SimpleNamespace(RATE_LIMIT_STOP_BELOW=20)
    target.pipeline = object()
    target.main = lambda: 0

    scope = types.ModuleType("statmaker_domestic_scope")
    scope.install_odds_registry_load_guard = lambda _pipeline: None

    integrity = types.ModuleType("odds_market_integrity")
    integrity.install_parser_guard = lambda _odds: None

    expansion = types.ModuleType("domestic_market_expansion_v18")
    expansion.install = lambda _odds, _pipeline: None

    spec = importlib.util.spec_from_file_location("refresh_domestic_odds_integrity_test_subject", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "refresh_domestic_live_july_odds": target,
            "statmaker_domestic_scope": scope,
            "odds_market_integrity": integrity,
            "domestic_market_expansion_v18": expansion,
        },
    ):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module


class FakeOddsModule:
    RATE_LIMIT_STOP_BELOW = 45

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.discover_provider_leagues = self._discover

    def _discover(self, _api_key, debug):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        debug["rateLimitRemaining"] = response["remaining"]
        debug["rateLimitReset"] = response.get("reset")
        return response.get("leagues", [f"call-{self.calls}"])

    @staticmethod
    def should_stop_for_rate_limit(debug):
        remaining = debug.get("rateLimitRemaining")
        return isinstance(remaining, int) and remaining < FakeOddsModule.RATE_LIMIT_STOP_BELOW


class FakeStaleOddsModule:
    def __init__(self):
        self.normalize_event_match = self._normalize_event_match
        self.output_debug = self._output_debug

    @staticmethod
    def event_id(event):
        return str(event.get("id") or "")

    @staticmethod
    def event_kickoff(event):
        return str(event.get("date") or "")

    @staticmethod
    def bookmaker_blocks(event_odds):
        raw = event_odds.get("bookmakers") or {}
        if isinstance(raw, dict):
            return [
                (name, [market for market in markets if isinstance(market, dict)])
                for name, markets in raw.items()
                if isinstance(markets, list)
            ]
        return []

    @staticmethod
    def _normalize_event_match(config_league, event, event_odds, aliases, debug):
        return {
            "id": str(event.get("id") or ""),
            "date": str(event.get("date") or "")[:10],
            "kickoff": str(event.get("date") or ""),
            "homeTeam": str(event.get("home") or ""),
            "awayTeam": str(event.get("away") or ""),
            "providerHomeTeam": str(event.get("home") or ""),
            "providerAwayTeam": str(event.get("away") or ""),
            "teamMappingStatus": "matched",
            "usableForStats": True,
            "markets": [
                {
                    "market": "TEAM_CORNERS",
                    "selection": "FC Lugano Corners Under 5.5",
                    "odds": 1.83,
                    "bookmaker": "Bet365",
                    "exactBookmakerOdds": True,
                }
            ],
        }

    @staticmethod
    def _output_debug(generated_at, debug):
        return {
            "generatedAt": generated_at,
            "warnings": list(debug.get("warnings", [])),
        }


class FakeRefreshModule:
    def __init__(self):
        self.safe_merge_odds_feed = self._safe_merge

    @staticmethod
    def _safe_merge(previous, fresh, registry, today):
        return copy.deepcopy(previous)


class DomesticRateLimitWaitGuardTest(unittest.TestCase):
    def setUp(self):
        self.subject = load_subject()
        self.now = dt.datetime(2026, 8, 4, 16, 29, 23, tzinfo=UTC)

    def install(self, odds, slept):
        self.subject.install_rate_limit_wait_guard(
            odds,
            sleep_fn=slept.append,
            now_fn=lambda: self.now,
        )

    def test_near_reset_waits_once_and_resumes(self):
        odds = FakeOddsModule([
            {
                "remaining": 27,
                "reset": "2026-08-04T16:34:24Z",
                "leagues": ["before-reset"],
            },
            {
                "remaining": 99,
                "reset": "2026-08-04T16:39:24Z",
                "leagues": ["after-reset"],
            },
        ])
        slept = []
        self.install(odds, slept)

        debug = {"warnings": []}
        result = odds.discover_provider_leagues("key", debug)

        self.assertEqual(["after-reset"], result)
        self.assertEqual([303], slept)
        self.assertEqual(2, odds.calls)
        self.assertEqual("resumed", debug["rateLimitWait"]["status"])
        self.assertEqual(27, debug["rateLimitWait"]["remainingBefore"])
        self.assertEqual(99, debug["rateLimitWait"]["remainingAfter"])

    def test_reset_outside_wait_window_does_not_sleep_or_retry(self):
        odds = FakeOddsModule([{
            "remaining": 27,
            "reset": "2026-08-04T16:50:00Z",
            "leagues": ["unchanged"],
        }])
        slept = []
        self.install(odds, slept)

        with patch.dict(os.environ, {"ODDS_API_IO_RATE_LIMIT_WAIT_MAX_SECONDS": "420"}):
            debug = {"warnings": []}
            result = odds.discover_provider_leagues("key", debug)

        self.assertEqual(["unchanged"], result)
        self.assertEqual([], slept)
        self.assertEqual(1, odds.calls)
        self.assertEqual("reset_outside_wait_window", debug["rateLimitWait"]["reason"])

    def test_healthy_remaining_does_not_wait(self):
        odds = FakeOddsModule([{
            "remaining": 70,
            "reset": "2026-08-04T16:34:24Z",
            "leagues": ["healthy"],
        }])
        slept = []
        self.install(odds, slept)

        debug = {"warnings": []}
        result = odds.discover_provider_leagues("key", debug)

        self.assertEqual(["healthy"], result)
        self.assertEqual([], slept)
        self.assertEqual(1, odds.calls)
        self.assertNotIn("rateLimitWait", debug)

    def test_invalid_reset_does_not_wait(self):
        odds = FakeOddsModule([{
            "remaining": 27,
            "reset": "not-a-reset",
            "leagues": ["unchanged"],
        }])
        slept = []
        self.install(odds, slept)

        debug = {"warnings": []}
        result = odds.discover_provider_leagues("key", debug)

        self.assertEqual(["unchanged"], result)
        self.assertEqual([], slept)
        self.assertEqual(1, odds.calls)
        self.assertEqual("missing_or_invalid_reset", debug["rateLimitWait"]["reason"])

    def test_guard_is_idempotent(self):
        odds = FakeOddsModule([{
            "remaining": 70,
            "reset": "2026-08-04T16:34:24Z",
            "leagues": ["healthy"],
        }])
        slept = []
        self.install(odds, slept)
        first_guard = odds.discover_provider_leagues
        self.install(odds, slept)

        self.assertIs(first_guard, odds.discover_provider_leagues)

    def test_iso_and_epoch_reset_formats(self):
        iso = self.subject._parse_reset_at("2026-08-04T16:34:24Z")
        epoch = self.subject._parse_reset_at(str(int(iso.timestamp())))
        milliseconds = self.subject._parse_reset_at(str(int(iso.timestamp() * 1000)))

        self.assertEqual(iso, epoch)
        self.assertEqual(iso, milliseconds)


class DomesticStaleImminentOddsGuardTest(unittest.TestCase):
    def setUp(self):
        self.subject = load_subject()
        self.now = dt.datetime(2026, 8, 23, 13, 45, 0, tzinfo=UTC)
        self.event = {
            "id": 72176896,
            "home": "FC Lugano",
            "away": "FC St. Gallen 1879",
            "date": "2026-08-23T14:30:00Z",
        }

    def odds_payload(self, updated_at):
        market = {
            "name": "Team Corners",
            "odds": [{"hdp": 5.5, "over": "1.90", "under": "1.83"}],
        }
        if updated_at is not None:
            market["updatedAt"] = updated_at
        return {"bookmakers": {"Bet365": [market]}}

    def install(self):
        odds = FakeStaleOddsModule()
        refresh = FakeRefreshModule()
        self.subject.install_stale_imminent_odds_guard(
            odds,
            refresh,
            now_fn=lambda: self.now,
        )
        return odds, refresh

    def test_lugano_shape_is_suppressed_when_newest_odds_are_four_days_old(self):
        odds, refresh = self.install()
        debug = {"warnings": []}
        payload = self.odds_payload("2026-08-19T15:26:50.399Z")

        match = odds.normalize_event_match(
            {"leagueCode": "SWZ"},
            self.event,
            payload,
            {},
            debug,
        )

        self.assertEqual([], match["markets"])
        self.assertTrue(match["bettingSuppressed"])
        self.assertEqual("stale_imminent_provider_odds", match["bettingSuppressionReason"])
        self.assertGreater(match["oddsFreshness"]["oddsAgeHours"], 72)
        self.assertEqual("72176896", debug["staleImminentOddsEvents"][0]["id"])

        fresh = {
            "debug": odds.output_debug("2026-08-23T13:45:00Z", debug),
        }
        previous = {
            "leagues": [
                {
                    "leagueCode": "SWZ",
                    "matches": [
                        {
                            "id": "72176896",
                            "homeTeam": "FC Lugano",
                            "awayTeam": "FC ST. Gallen",
                            "markets": [{"odds": 1.83}],
                        },
                        {
                            "id": "keep-me",
                            "homeTeam": "FC Zurich",
                            "awayTeam": "FC Basel",
                            "markets": [{"odds": 1.91}],
                        },
                    ],
                }
            ],
            "debug": {},
        }

        merged = refresh.safe_merge_odds_feed(previous, fresh, [], self.now.date())
        ids = [match["id"] for match in merged["leagues"][0]["matches"]]
        self.assertEqual(["keep-me"], ids)
        self.assertEqual(1, merged["debug"]["staleImminentOddsPurgedAfterMerge"])
        self.assertEqual(["72176896"], merged["debug"]["staleImminentOddsRejectedIds"])

    def test_recent_odds_are_not_suppressed(self):
        odds, _refresh = self.install()
        debug = {"warnings": []}
        payload = self.odds_payload("2026-08-23T12:45:00Z")

        match = odds.normalize_event_match(
            {"leagueCode": "SWZ"},
            self.event,
            payload,
            {},
            debug,
        )

        self.assertTrue(match["markets"])
        self.assertNotIn("bettingSuppressed", match)
        self.assertNotIn("staleImminentOddsEvents", debug)

    def test_old_odds_are_not_suppressed_outside_imminent_window(self):
        odds, _refresh = self.install()
        debug = {"warnings": []}
        event = dict(self.event)
        event["date"] = "2026-08-25T14:30:00Z"
        payload = self.odds_payload("2026-08-19T15:26:50.399Z")

        match = odds.normalize_event_match(
            {"leagueCode": "SWZ"},
            event,
            payload,
            {},
            debug,
        )

        self.assertTrue(match["markets"])
        self.assertNotIn("bettingSuppressed", match)

    def test_missing_provider_timestamps_fail_open_for_freshness_only(self):
        odds, _refresh = self.install()
        debug = {"warnings": []}

        match = odds.normalize_event_match(
            {"leagueCode": "SWZ"},
            self.event,
            self.odds_payload(None),
            {},
            debug,
        )

        self.assertTrue(match["markets"])
        self.assertNotIn("bettingSuppressed", match)

    def test_stale_guard_is_idempotent(self):
        odds = FakeStaleOddsModule()
        refresh = FakeRefreshModule()
        self.subject.install_stale_imminent_odds_guard(odds, refresh, now_fn=lambda: self.now)
        first_normalize = odds.normalize_event_match
        first_merge = refresh.safe_merge_odds_feed

        self.subject.install_stale_imminent_odds_guard(odds, refresh, now_fn=lambda: self.now)

        self.assertIs(first_normalize, odds.normalize_event_match)
        self.assertIs(first_merge, refresh.safe_merge_odds_feed)


if __name__ == "__main__":
    unittest.main()
