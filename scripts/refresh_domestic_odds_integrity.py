#!/usr/bin/env python3
"""Run the Domestic odds refresh with integrity, scope, and reset-window guards."""
from __future__ import annotations

import datetime as dt
import math
import os
import time
from typing import Any, Callable, MutableMapping, Optional

import refresh_domestic_live_july_odds as target
import statmaker_domestic_scope as scope
from odds_market_integrity import install_parser_guard


DEFAULT_DOMESTIC_RATE_LIMIT_RESERVE = 45
DEFAULT_RATE_LIMIT_WAIT_MAX_SECONDS = 420
DEFAULT_RATE_LIMIT_WAIT_BUFFER_SECONDS = 2
_RATE_LIMIT_WAIT_GUARD_MARKER = "_statmaker_rate_limit_wait_guard"


def _non_negative_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _parse_reset_at(value: Any) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None

    try:
        timestamp = float(text)
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000.0
        return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        pass

    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def seconds_until_rate_limit_reset(
    debug: MutableMapping[str, Any],
    *,
    now: Optional[dt.datetime] = None,
) -> Optional[float]:
    reset_at = _parse_reset_at(debug.get("rateLimitReset"))
    if reset_at is None:
        return None
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)
    return max(0.0, (reset_at - current).total_seconds())


def install_rate_limit_wait_guard(
    odds_module: Any,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
) -> None:
    """Retry league discovery once when the provider reset is only minutes away.

    The existing reserve remains authoritative. This guard does not lower it and
    never loops: it waits only after league discovery, then retries discovery one
    time. If the provider is still below reserve, the existing safe-preservation
    path keeps the last valid feed.
    """
    original = odds_module.discover_provider_leagues
    if getattr(original, _RATE_LIMIT_WAIT_GUARD_MARKER, False):
        return

    def guarded(api_key: str, debug: MutableMapping[str, Any]):
        provider_leagues = original(api_key, debug)
        if not odds_module.should_stop_for_rate_limit(debug):
            return provider_leagues

        max_wait = _non_negative_env_int(
            "ODDS_API_IO_RATE_LIMIT_WAIT_MAX_SECONDS",
            DEFAULT_RATE_LIMIT_WAIT_MAX_SECONDS,
        )
        buffer_seconds = _non_negative_env_int(
            "ODDS_API_IO_RATE_LIMIT_WAIT_BUFFER_SECONDS",
            DEFAULT_RATE_LIMIT_WAIT_BUFFER_SECONDS,
        )
        seconds_to_reset = seconds_until_rate_limit_reset(debug, now=now_fn())
        remaining_before = debug.get("rateLimitRemaining")

        if seconds_to_reset is None:
            debug["rateLimitWait"] = {
                "status": "skipped",
                "reason": "missing_or_invalid_reset",
                "remainingBefore": remaining_before,
                "reset": debug.get("rateLimitReset"),
            }
            return provider_leagues

        wait_seconds = int(math.ceil(seconds_to_reset + buffer_seconds))
        if wait_seconds <= 0 or wait_seconds > max_wait:
            debug["rateLimitWait"] = {
                "status": "skipped",
                "reason": "reset_outside_wait_window",
                "remainingBefore": remaining_before,
                "reset": debug.get("rateLimitReset"),
                "secondsToReset": round(seconds_to_reset, 3),
                "calculatedWaitSeconds": wait_seconds,
                "maxWaitSeconds": max_wait,
            }
            return provider_leagues

        debug["rateLimitWait"] = {
            "status": "waiting",
            "reason": "reserve_reached_near_reset",
            "remainingBefore": remaining_before,
            "reset": debug.get("rateLimitReset"),
            "secondsToReset": round(seconds_to_reset, 3),
            "waitSeconds": wait_seconds,
            "maxWaitSeconds": max_wait,
            "retryLimit": 1,
        }
        debug.setdefault("warnings", []).append(
            f"Domestic odds reserve reached ({remaining_before}); waiting {wait_seconds}s "
            "for the provider reset, then retrying league discovery once."
        )
        sleep_fn(wait_seconds)

        provider_leagues = original(api_key, debug)
        remaining_after = debug.get("rateLimitRemaining")
        resumed = not odds_module.should_stop_for_rate_limit(debug)
        debug["rateLimitWait"].update({
            "status": "resumed" if resumed else "still_below_reserve",
            "remainingAfter": remaining_after,
            "retryCount": 1,
        })
        debug.setdefault("warnings", []).append(
            "Domestic odds refresh resumed after rate-limit reset."
            if resumed
            else "Domestic odds refresh remains below reserve after one reset retry; preserving the prior feed."
        )
        return provider_leagues

    setattr(guarded, _RATE_LIMIT_WAIT_GUARD_MARKER, True)
    odds_module.discover_provider_leagues = guarded


def main() -> int:
    install_parser_guard(target.odds_fetch)
    scope.install_odds_registry_load_guard(target.pipeline)

    # Domestic and UEFA refreshes share the same Odds-API.io key in one workflow.
    # Preserve a meaningful provider reset-window reserve for the UEFA stage.
    reserve = _non_negative_env_int(
        "ODDS_API_IO_DOMESTIC_RATE_LIMIT_RESERVE",
        DEFAULT_DOMESTIC_RATE_LIMIT_RESERVE,
    )
    target.odds_fetch.RATE_LIMIT_STOP_BELOW = max(
        int(target.odds_fetch.RATE_LIMIT_STOP_BELOW),
        max(1, reserve),
    )
    install_rate_limit_wait_guard(target.odds_fetch)
    return target.main()


if __name__ == "__main__":
    raise SystemExit(main())
