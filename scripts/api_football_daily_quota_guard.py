#!/usr/bin/env python3
"""Provider-header daily reserve guard for API-Football Domestic Stats workloads.

The guard makes no extra API request. It reads API-Sports daily quota headers from
normal responses and refuses the next Stats/history/settlement request once the
remaining daily allowance reaches the configured reserve.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

DEFAULT_DAILY_RESERVE = 1500
_STATE: Dict[str, Any] = {
    "dailyLimit": None,
    "dailyRemaining": None,
    "reserve": DEFAULT_DAILY_RESERVE,
    "stoppedAtReserve": False,
}


def _non_negative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def configured_reserve() -> int:
    return _non_negative_int(
        os.getenv("API_FOOTBALL_DAILY_RESERVE", str(DEFAULT_DAILY_RESERVE)),
        DEFAULT_DAILY_RESERVE,
    )


def _header_int(headers: Any, *names: str) -> int | None:
    if headers is None:
        return None
    for name in names:
        value = headers.get(name)
        if value is None:
            continue
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            continue
    return None


def _capture(headers: Any) -> None:
    remaining = _header_int(
        headers,
        "x-ratelimit-requests-remaining",
        "X-RateLimit-Requests-Remaining",
        "x-ratelimit-remaining",
        "X-RateLimit-Remaining",
    )
    limit = _header_int(
        headers,
        "x-ratelimit-requests-limit",
        "X-RateLimit-Requests-Limit",
        "x-ratelimit-limit",
        "X-RateLimit-Limit",
    )
    if remaining is not None:
        _STATE["dailyRemaining"] = remaining
    if limit is not None:
        _STATE["dailyLimit"] = limit


def status() -> Dict[str, Any]:
    return dict(_STATE)


def install(stats_fetch: Any) -> None:
    """Install once on ``api_football_fetch_fixture_stats.api_get``."""
    current = stats_fetch.api_get
    if getattr(current, "_statmaker_daily_quota_guard", False):
        return

    def guarded_api_get(
        api_key: str,
        endpoint: str,
        params: Dict[str, Any],
        request_state: Dict[str, int],
        max_requests: int,
    ) -> Dict[str, Any]:
        reserve = configured_reserve()
        _STATE["reserve"] = reserve
        remaining = _STATE.get("dailyRemaining")
        if isinstance(remaining, int) and remaining <= reserve:
            _STATE["stoppedAtReserve"] = True
            raise stats_fetch.RequestLimitReached(
                f"API-Football daily reserve reached: remaining={remaining} reserve={reserve}"
            )

        if request_state["count"] >= max_requests:
            raise stats_fetch.RequestLimitReached

        query = stats_fetch.urlencode(
            {key: value for key, value in params.items() if value is not None and value != ""}
        )
        url = (
            f"{stats_fetch.BASE_URL}/{endpoint}?{query}"
            if query
            else f"{stats_fetch.BASE_URL}/{endpoint}"
        )
        request = stats_fetch.Request(
            url,
            headers={
                "x-apisports-key": api_key,
                "Accept": "application/json",
                "User-Agent": "StatMaker-Data API-Football cache",
            },
            method="GET",
        )

        request_state["count"] += 1
        with stats_fetch.urlopen(request, timeout=stats_fetch.TIMEOUT_SECONDS) as response:
            _capture(response.headers)
            payload = json.loads(response.read().decode("utf-8"))

        stats_fetch.time.sleep(stats_fetch.REQUEST_DELAY_SECONDS)
        return payload

    setattr(guarded_api_get, "_statmaker_daily_quota_guard", True)
    stats_fetch.api_get = guarded_api_get
