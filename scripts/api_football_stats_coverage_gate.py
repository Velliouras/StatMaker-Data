"""Bounded API-Football statistics-coverage preflight for existing league seasons.

The official `leagues?id=&season=` response describes whether fixture-level
statistics are supported. FALSE prevents wasted per-fixture requests; UNKNOWN
never masquerades as unsupported. Cache results with an exact source scope.
No odds, betting predictions, or changes to the list of enabled leagues.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import api_football_fetch_fixture_stats as api


def _utc(value: Any):
    try:
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _identity(league):
    try:
        league_id = int(league.get("api_football_league_id") or league.get("apiFootballLeagueId"))
        season = int(league.get("season"))
    except (TypeError, ValueError):
        return None
    return league_id, season


def cached_coverage_status(league, entry, now=None):
    ident = _identity(league)
    if not ident or not isinstance(entry, dict):
        return None
    if entry.get("leagueId") != ident[0] or entry.get("season") != ident[1]:
        return None
    checked = _utc(entry.get("checkedAtUtc"))
    if not checked:
        return None
    now = now or datetime.now(timezone.utc)
    ttl = timedelta(days=7 if entry.get("statisticsCovered") is False else 30)
    if now < checked or now - checked > ttl:
        return None
    return entry if isinstance(entry.get("statisticsCovered"), bool) else None


def read_statistics_coverage(league, previous, api_key, request_state, max_requests):
    """Return (official status or None, updated cache entry, diagnostic reason).

    Never infers coverage from an empty fixture response or a nonmatching
    competition. The API quota guard remains authoritative.
    """
    found = cached_coverage_status(league, previous)
    if found:
        return found["statisticsCovered"], found, "OFFICIAL_CACHED_COVERAGE"
    ident = _identity(league)
    if not ident:
        return None, previous, "MISSING_PROVIDER_LEAGUE_SEASON_ID"
    if request_state["count"] >= max_requests:
        return None, previous, "REQUEST_BUDGET_EXHAUSTED_BEFORE_COVERAGE"
    try:
        payload = api.api_get(api_key, "leagues", {"id": ident[0], "season": ident[1]},
                              request_state, max_requests)
    except (api.RequestLimitReached, api.HTTPError, api.URLError) as exc:
        return None, previous, "COVERAGE_REQUEST_FAILED_" + type(exc).__name__
    if api.api_errors(payload):
        return None, previous, "COVERAGE_PROVIDER_ERROR"
    matches = [item for item in api.response_items(payload)
               if str((item.get("league") or {}).get("id")) == str(ident[0])]
    if len(matches) != 1:
        return None, previous, "UNVERIFIED_PROVIDER_LEAGUE_ID"
    seasons = [s for s in matches[0].get("seasons") or []
               if str(s.get("year")) == str(ident[1])]
    if len(seasons) != 1:
        return None, previous, "UNVERIFIED_PROVIDER_SEASON"
    flags = ((seasons[0].get("coverage") or {}).get("fixtures") or {})
    value = flags.get("statistics_fixtures")
    if not isinstance(value, bool):
        return None, previous, "MISSING_STATISTICS_COVERAGE_FLAG"
    entry = {"leagueId": ident[0], "season": ident[1],
             "statisticsCovered": value,
             "checkedAtUtc": api.now_utc(),
             "source": "API_FOOTBALL_LEAGUES_SEASON_COVERAGE"}
    return value, entry, ("OFFICIAL_STATISTICS_AVAILABLE" if value else
                          "OFFICIAL_STATISTICS_NOT_COVERED")
