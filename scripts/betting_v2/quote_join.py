#!/usr/bin/env python3
"""Strict offline join of untouched model fixture to a historic pregame quote.

Fixture dates in model archives are UTC; odds feeds are grouped by Athens day.
Never join on those different calendar dates. Require verified API fixture ID,
same league, correct kickoff and an as-of price strictly before kickoff.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ATHENS = ZoneInfo("Europe/Athens")


def aware_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def forecast_index(forecasts: list[dict]) -> dict[tuple[str, str], dict]:
    index = {}
    for item in forecasts:
        fixture_id = str(item.get("fixtureId") or "")
        league = str(item.get("leagueCode") or "")
        if not fixture_id or not league or aware_utc(item.get("kickoffUTC")) is None:
            raise ValueError("Forecast fixture lacks verified ID, league, or UTC kickoff")
        key = (league, fixture_id)
        if key in index:
            raise ValueError("Duplicate fixture in forecast split")
        index[key] = item
    return index


def exact_join(quote: dict, indexed: dict) -> tuple[dict | None, str]:
    if quote.get("identityResolution") != "EXACT_CACHED_FIXTURE":
        return None, "UNVERIFIED_PRICE_FIXTURE_IDENTITY"
    code = str(quote.get("leagueCode") or "")
    fixture = str(quote.get("fixtureId") or "")
    match = indexed.get((code, fixture))
    if match is None:
        return None, "NO_UNTOUCHED_FORECAST_FOR_IDENTICAL_LEAGUE_FIXTURE"
    market_kickoff = aware_utc(quote.get("kickoffUTC"))
    model_kickoff = aware_utc(match.get("kickoffUTC"))
    cutoff = aware_utc(quote.get("quoteCutoff"))
    if market_kickoff is None or model_kickoff is None or cutoff is None:
        return None, "UNKNOWN_PRICE_OR_KICKOFF_TIMESTAMP"
    if abs((market_kickoff - model_kickoff).total_seconds()) > 900:
        return None, "KICKOFF_MISMATCH"
    if cutoff >= market_kickoff or cutoff >= model_kickoff:
        return None, "PRICE_NOT_PRE_KICKOFF"
    # Odds exports are keyed to Athens calendar dates; forecast history uses UTC.
    if market_kickoff.astimezone(ATHENS).date().isoformat() != quote.get("date"):
        return None, "PRICE_NOT_FOR_LOCAL_MATCH_DAY"
    return match, "EXACT_PREMATCH_JOIN"
