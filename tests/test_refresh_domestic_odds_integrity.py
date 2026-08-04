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

    spec = importlib.util.spec_from_file_location("refresh_domestic_odds_integrity_test_subject", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "refresh_domestic_live_july_odds": target,
            "statmaker_domestic_scope": scope,
            "odds_market_integrity": integrity,
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


if __name__ == "__main__":
    unittest.main()
