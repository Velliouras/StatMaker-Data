#!/usr/bin/env python3
"""Run the Domestic odds refresh with integrity, scope, reset-window, and stale-event guards."""
from __future__ import annotations

import datetime as dt
import math
import os
import time
from typing import Any, Callable, MutableMapping, Optional

import domestic_market_expansion_v18
import refresh_domestic_live_july_odds as target
import statmaker_domestic_scope as scope
from odds_market_integrity import install_parser_guard


DEFAULT_DOMESTIC_RATE_LIMIT_RESERVE = 45
DEFAULT_RATE_LIMIT_WAIT_MAX_SECONDS = 420
DEFAULT_RATE_LIMIT_WAIT_BUFFER_SECONDS = 2
DEFAULT_STALE_ODDS_MAX_AGE_HOURS = 72
DEFAULT_STALE_ODDS_KICKOFF_WINDOW_HOURS = 24
STALE_ODDS_PAST_KICKOFF_GRACE_HOURS = 6
_RATE_LIMIT_WAIT_GUARD_MARKER = "_statmaker_rate_limit_wait_guard"
_STALE_EVENT_GUARD_MARKER = "_statmaker_stale_imminent_odds_guard"


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


def _parse_provider_datetime(value: Any) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _latest_provider_market_update(odds_module: Any, event_odds: Any) -> Optional[dt.datetime]:
    if not isinstance(event_odds, dict):
        return None

    candidates = []
    for key in ("updatedAt", "updated_at", "lastUpdatedAt", "lastUpdate", "updated"):
        parsed = _parse_provider_datetime(event_odds.get(key))
        if parsed is not None:
            candidates.append(parsed)

    try:
        blocks = odds_module.bookmaker_blocks(event_odds)
    except (AttributeError, TypeError, ValueError):
        blocks = []

    for _bookmaker, markets in blocks:
        for market in markets:
            if not isinstance(market, dict):
                continue
            for key in ("updatedAt", "updated_at", "lastUpdatedAt", "lastUpdate", "updated"):
                parsed = _parse_provider_datetime(market.get(key))
                if parsed is not None:
                    candidates.append(parsed)

    return max(candidates) if candidates else None


def stale_imminent_odds_reason(
    odds_module: Any,
    event: Any,
    event_odds: Any,
    *,
    now: Optional[dt.datetime] = None,
) -> Optional[dict[str, Any]]:
    """Return fail-closed suppression metadata for clearly stale imminent odds.

    This guard deliberately uses only data already returned by Odds-API.io. It adds
    zero provider calls. A fixture is suppressed only when all of these are true:
    - kickoff is within the configured imminent window (or only just passed);
    - at least one provider market timestamp is available;
    - the newest market timestamp is older than the configured stale threshold.

    Missing timestamps do not trigger suppression because freshness cannot be
    established safely.
    """
    if not isinstance(event, dict) or not isinstance(event_odds, dict):
        return None

    kickoff = _parse_provider_datetime(odds_module.event_kickoff(event))
    latest_update = _latest_provider_market_update(odds_module, event_odds)
    if kickoff is None or latest_update is None:
        return None

    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)

    window_hours = _non_negative_env_int(
        "STATMAKER_DOMESTIC_STALE_ODDS_KICKOFF_WINDOW_HOURS",
        DEFAULT_STALE_ODDS_KICKOFF_WINDOW_HOURS,
    )
    max_age_hours = _non_negative_env_int(
        "STATMAKER_DOMESTIC_STALE_ODDS_MAX_AGE_HOURS",
        DEFAULT_STALE_ODDS_MAX_AGE_HOURS,
    )

    hours_to_kickoff = (kickoff - current).total_seconds() / 3600.0
    if hours_to_kickoff < -STALE_ODDS_PAST_KICKOFF_GRACE_HOURS or hours_to_kickoff > window_hours:
        return None

    age_hours = max(0.0, (current - latest_update).total_seconds() / 3600.0)
    if age_hours <= max_age_hours:
        return None

    return {
        "policy": "fail_closed_stale_imminent_provider_odds",
        "latestOddsUpdatedAt": latest_update.isoformat().replace("+00:00", "Z"),
        "oddsAgeHours": round(age_hours, 2),
        "hoursToKickoff": round(hours_to_kickoff, 2),
        "maxOddsAgeHours": max_age_hours,
        "kickoffWindowHours": window_hours,
        "extraApiCalls": 0,
    }


def install_stale_imminent_odds_guard(
    odds_module: Any,
    refresh_module: Any,
    *,
    now_fn: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
) -> None:
    """Suppress stale imminent betting events and prevent safe-merge resurrection.

    The rotating refresh intentionally preserves prior unexpired matches after
    empty/partial refreshes. For a provider event explicitly rejected by this
    freshness guard, that preservation would resurrect the stale betting markets.
    Therefore the merge wrapper removes only the exact rejected event ids after the
    normal safe merge. The schedule overlay may still keep the fixture as
    schedule-only with markets=[], which is safe for the Android betting engine.
    """
    if getattr(odds_module, _STALE_EVENT_GUARD_MARKER, False):
        return

    original_normalize_event_match = odds_module.normalize_event_match
    original_output_debug = odds_module.output_debug
    original_safe_merge = refresh_module.safe_merge_odds_feed

    def guarded_normalize_event_match(
        config_league: dict[str, Any],
        event: dict[str, Any],
        event_odds: Optional[dict[str, Any]],
        aliases: dict[str, dict[str, str]],
        debug: dict[str, Any],
    ):
        match = original_normalize_event_match(config_league, event, event_odds, aliases, debug)
        if match is None:
            return None

        reason = stale_imminent_odds_reason(
            odds_module,
            event,
            event_odds,
            now=now_fn(),
        )
        if reason is None:
            return match

        event_key = str(odds_module.event_id(event) or match.get("id") or "").strip()
        home = str(match.get("homeTeam") or match.get("providerHomeTeam") or "").strip()
        away = str(match.get("awayTeam") or match.get("providerAwayTeam") or "").strip()
        rejection = {
            "id": event_key,
            "leagueCode": str(config_league.get("leagueCode") or ""),
            "fixture": f"{home} - {away}".strip(" -"),
            "kickoff": match.get("kickoff"),
            **reason,
        }
        debug.setdefault("staleImminentOddsEvents", []).append(rejection)
        debug.setdefault("warnings", []).append(
            f"Suppressed stale imminent odds for {rejection['leagueCode']} "
            f"{rejection['fixture']} ({event_key}); newest market update is "
            f"{reason['oddsAgeHours']}h old."
        )

        match["markets"] = []
        match["bettingSuppressed"] = True
        match["bettingSuppressionReason"] = "stale_imminent_provider_odds"
        match["oddsFreshness"] = reason
        return match

    def guarded_output_debug(generated_at: str, debug: dict[str, Any]) -> dict[str, Any]:
        result = original_output_debug(generated_at, debug)
        result["staleImminentOddsEvents"] = list(debug.get("staleImminentOddsEvents", []) or [])
        result["staleImminentOddsPolicy"] = {
            "maxOddsAgeHours": _non_negative_env_int(
                "STATMAKER_DOMESTIC_STALE_ODDS_MAX_AGE_HOURS",
                DEFAULT_STALE_ODDS_MAX_AGE_HOURS,
            ),
            "kickoffWindowHours": _non_negative_env_int(
                "STATMAKER_DOMESTIC_STALE_ODDS_KICKOFF_WINDOW_HOURS",
                DEFAULT_STALE_ODDS_KICKOFF_WINDOW_HOURS,
            ),
            "pastKickoffGraceHours": STALE_ODDS_PAST_KICKOFF_GRACE_HOURS,
            "extraApiCalls": 0,
        }
        return result

    def guarded_safe_merge(previous, fresh, registry, today):
        merged = original_safe_merge(previous, fresh, registry, today)
        rejected = list((fresh.get("debug") or {}).get("staleImminentOddsEvents", []) or [])
        rejected_ids = {
            str(row.get("id") or "").strip()
            for row in rejected
            if str(row.get("id") or "").strip()
        }
        if not rejected_ids:
            return merged

        purged = 0
        for league in merged.get("leagues", []) or []:
            if not isinstance(league, dict):
                continue
            kept = []
            for match in league.get("matches", []) or []:
                if not isinstance(match, dict):
                    continue
                match_id = str(match.get("id") or match.get("matchId") or "").strip()
                if match_id and match_id in rejected_ids:
                    purged += 1
                    continue
                kept.append(match)
            league["matches"] = kept

        merged.setdefault("debug", {})["staleImminentOddsPurgedAfterMerge"] = purged
        merged["debug"]["staleImminentOddsRejectedIds"] = sorted(rejected_ids)
        return merged

    odds_module.normalize_event_match = guarded_normalize_event_match
    odds_module.output_debug = guarded_output_debug
    refresh_module.safe_merge_odds_feed = guarded_safe_merge
    setattr(odds_module, _STALE_EVENT_GUARD_MARKER, True)


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

    # Install in the same order as target.main(), then add the cumulative v18
    # Asian-family wrapper last. target.main() sees the installer guards and keeps
    # this exact order.
    target.domestic_odds_expansion.install(target.odds_fetch, target.pipeline)
    domestic_market_expansion_v18.install(target.odds_fetch, target.pipeline)

    # Fail closed on clearly stale imminent provider odds. This uses only timestamps
    # already present in the Odds-API.io payload and therefore consumes no extra API
    # quota. It also prevents the rotating safe-merge from resurrecting a rejected
    # stale event.
    install_stale_imminent_odds_guard(target.odds_fetch, target)

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
