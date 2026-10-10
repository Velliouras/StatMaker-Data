#!/usr/bin/env python3
"""Fail-closed individual bookmaker quote provenance for V2 priced calibration.

A Git snapshot cutoff or bundle creation timestamp is NOT a bookmaker quote
observation timestamp. Reject prepared selections and selected-only universes.
This is a prerequisite, never a model or profit certificate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite

MAX_QUOTE_AGE = timedelta(hours=24)
PRICE_UNIVERSE = "INDEPENDENT_VERIFIED_BOOKMAKER_OFFERS"


def parse_utc(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None


def verified_offer_reason(quote: dict) -> str | None:
    """None only if this *specific* quote's time, ID and universe are verified."""
    if (quote.get("priceObservationTimestampVerified") is not True or
            quote.get("independentUnfilteredBookmakerUniverseVerified") is not True or
            quote.get("priceUniverse") != PRICE_UNIVERSE):
        return "UNVERIFIED_INDIVIDUAL_BOOKMAKER_QUOTE_OR_PRICE_UNIVERSE"
    for key in ("bookmaker", "bookmakerMarketSelectionId", "sourceGenerationId"):
        if not isinstance(quote.get(key), str) or not quote[key].strip():
            return "MISSING_BOOKMAKER_SELECTION_OR_SOURCE_GENERATION"
    observed = parse_utc(quote.get("quoteObservedAt"))
    kickoff = parse_utc(quote.get("kickoffUTC"))
    cutoff = parse_utc(quote.get("quoteCutoff"))
    if observed is None or kickoff is None or cutoff is None:
        return "MISSING_ABSOLUTE_QUOTE_OBSERVATION_OR_KICKOFF"
    if not (observed <= cutoff < kickoff):
        return "QUOTE_OBSERVED_AFTER_CUTOFF_OR_KICKOFF"
    age = kickoff - observed
    if not timedelta(0) < age <= MAX_QUOTE_AGE:
        return "STALE_OR_FUTURE_QUOTE_OBSERVATION"
    try:
        odd = float(quote.get("odd"))
    except (TypeError, ValueError):
        return "INVALID_EXACT_BOOKMAKER_ODD"
    if not isfinite(odd) or not 1.8 < odd < 3.0:
        return "INVALID_EXACT_BOOKMAKER_ODD"
    return None
