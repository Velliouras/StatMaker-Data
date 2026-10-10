#!/usr/bin/env python3
"""Fail-closed matching of REAL pre-kickoff odds receipts to research forecasts.

Only exact team identities, absolute kickoffs and unique cached fixture IDs
may join. No fixture guessing, no unverified bookmaker quote timestamps,
no new provider calls, no artificial EV/ROI or STRONG publication.

The cache contains completed fixtures; future events are *not yet* covered by
this resolver. This explicitly reports them as unresolved until an independently
verified API-Football fixture crosswalk exists.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import json
from math import isfinite
from pathlib import Path

from fixture_lookup import CachedFixtureLookup, canonical_name
from forecast_market import market_probability
from index_quote_receipts import (
    MAX_GZIP_FILE_BYTES, MAX_RECEIPT_FILES, extracted_offers,
)
from quote_join import aware_utc, exact_join, forecast_index
from quote_receipt_capture import STATUS_PATH

SUPPORTED = frozenset({
    "RESULT_1X2", "RESULT_DNB", "RESULT_DOUBLE_CHANCE",
    "FULL_TIME_MATCH_TOTAL", "HOME_TEAM_TOTAL", "AWAY_TEAM_TOTAL",
})
ALLOWED_LINES = frozenset({0.5, 1.5, 2.5, 3.5, 4.5, 5.5})


def normalized_market(offer: dict) -> tuple[dict | None, str]:
    """Translate only unambiguous FULL-TIME V2 selections.

    Never include Asian/spread, handicap, HT, player, corner or card markets
    that have no corresponding calibrated xG/ELO goal probability model.
    """
    name = str(offer.get("providerMarketName") or "").casefold().strip()
    selection = str(offer.get("selection") or "").upper().strip()
    identity: dict = {}
    if name in ("ml", "1x2", "match result", "full time result"):
        mapping = {"HOME": "HOME", "DRAW": "DRAW", "AWAY": "AWAY"}
        if selection not in mapping:
            return None, "UNKNOWN_FULL_TIME_1X2_SELECTION"
        identity = {"market": "RESULT_1X2", "direction": mapping[selection]}
    elif name == "draw no bet":
        if selection not in ("HOME", "AWAY"):
            return None, "UNKNOWN_DNB_SELECTION"
        identity = {"market": "RESULT_DNB", "direction": selection}
    elif name == "double chance":
        directions = {"1X": "HOME_OR_DRAW", "X2": "AWAY_OR_DRAW",
                      "12": "HOME_OR_AWAY"}
        if selection not in directions:
            return None, "UNKNOWN_DOUBLE_CHANCE_SELECTION"
        identity = {"market": "RESULT_DOUBLE_CHANCE", "direction": directions[selection]}
    elif name in ("totals", "goals over/under"):
        identity = {"market": "FULL_TIME_MATCH_TOTAL", "direction": selection}
    elif name in ("team total home", "team goals home"):
        identity = {"market": "HOME_TEAM_TOTAL", "direction": selection, "teamSide": "HOME"}
    elif name in ("team total away", "team goals away"):
        identity = {"market": "AWAY_TEAM_TOTAL", "direction": selection, "teamSide": "AWAY"}
    else:
        return None, "UNSUPPORTED_OR_NON_FULL_TIME_MARKET"

    if identity["market"] not in SUPPORTED:
        return None, "UNSUPPORTED_MARKET"
    if identity["market"].endswith("TOTAL"):
        if selection not in ("OVER", "UNDER"):
            return None, "UNKNOWN_GOAL_TOTAL_SELECTION"
        value = offer.get("line")
        try:
            line = float(value) if not isinstance(value, bool) else None
        except (ValueError, TypeError, OverflowError):
            line = None
        if line not in ALLOWED_LINES:
            return None, "UNSUPPORTED_NON_HALF_GOAL_LINE"
        identity["line"] = line
    try:
        odd = float(offer.get("odd"))
    except (ValueError, TypeError, OverflowError):
        return None, "INVALID_EXACT_PRICE"
    if not isfinite(odd) or not 1.8 < odd < 3.0:
        return None, "OUTSIDE_STRONG_ODDS_RANGE"
    identity["odd"] = odd
    return identity, "MAPPED_FULL_TIME_RESEARCH_MARKET"


def fixture_resolution(
    fixture: dict, lookup: CachedFixtureLookup,
) -> tuple[dict | None, str]:
    """Unique exact home+away+UTC time across *all* cached leagues."""
    home = canonical_name(fixture.get("home") or fixture.get("homeTeam"))
    away = canonical_name(fixture.get("away") or fixture.get("awayTeam"))
    kickoff = aware_utc(
        fixture.get("date") or fixture.get("kickoff") or fixture.get("startTime")
    )
    if not home or not away or kickoff is None:
        return None, "MISSING_TEAM_OR_ABSOLUTE_KICKOFF"
    candidates: dict[tuple[str, str], dict] = {}
    for (league, h, a), matches in lookup.by_league_team.items():
        if h != home or a != away:
            continue
        for item in matches:
            if abs((item["kickoffUTC"] - kickoff).total_seconds()) <= 900:
                candidates[(league, item["fixtureId"])] = item
    if not candidates:
        return None, "NO_EXACT_CACHED_API_FIXTURE"
    if len(candidates) > 1:
        return None, "AMBIGUOUS_EXACT_API_FIXTURE"
    item = next(iter(candidates.values()))
    return item, "EXACT_CACHED_API_FIXTURE"


def provider_schedule_index(root: Path) -> dict[str, list[dict]]:
    """Read actual canonical Odds-API.io fixture schedule, not model results."""
    source = root / "odds/odds_api_io/domestic_odds.json"
    if not source.is_file():
        return {}
    feed = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(feed, dict):
        raise ValueError("Provider schedule not an object")
    by_id: dict[str, list[dict]] = defaultdict(list)
    for league in feed.get("leagues") or []:
        if not isinstance(league, dict):
            continue
        league_code = str(league.get("leagueCode") or "").strip()
        if not league_code:
            continue
        for match in league.get("matches") or []:
            if not isinstance(match, dict):
                continue
            identity = str(match.get("id") or "").strip()
            if identity:
                by_id[identity].append({
                    "leagueCode": league_code,
                    "kickoffUTC": match.get("kickoff"),
                    "providerHomeTeam": match.get("providerHomeTeam"),
                    "providerAwayTeam": match.get("providerAwayTeam"),
                    "providerEventId": identity,
                    "source": "odds-api-io-domestic-schedule",
                })
    return dict(by_id)


def provider_schedule_resolution(event: dict, by_id: dict) -> tuple[dict | None, str]:
    """Exact event ID, both unaltered teams and kickoff, one unique league."""
    eid = str(event.get("id") or event.get("eventId") or "").strip()
    h = canonical_name(event.get("home") or event.get("homeTeam"))
    a = canonical_name(event.get("away") or event.get("awayTeam"))
    kickoff = aware_utc(event.get("date") or event.get("kickoff") or event.get("startTime"))
    if not eid or not h or not a or kickoff is None:
        return None, "MISSING_PROVIDER_EVENT_TIME_OR_TEAMS"
    valid = {}
    for item in by_id.get(eid, []):
        ikick = aware_utc(item.get("kickoffUTC"))
        if (ikick is None or
                abs((ikick - kickoff).total_seconds()) > 900 or
                canonical_name(item.get("providerHomeTeam")) != h or
                canonical_name(item.get("providerAwayTeam")) != a):
            continue
        identity = (item["leagueCode"], eid)
        valid[identity] = item
    if not valid:
        return None, "NO_EXACT_PROVIDER_SCHEDULE_FIXTURE"
    if len(valid) > 1:
        return None, "AMBIGUOUS_PROVIDER_SCHEDULE_FIXTURE"
    return next(iter(valid.values())), "EXACT_PROVIDER_SCHEDULE_ONLY_NOT_API_FIXTURE"


def _load_forecasts(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {}
    with path.open(encoding="utf-8") as src:
        return forecast_index([json.loads(x) for x in src if x.strip()])


def study(root: Path, primary: Path | None = None,
          fallback: Path | None = None, *, max_files: int = MAX_RECEIPT_FILES,
          lookup: CachedFixtureLookup | None = None) -> dict:
    """Inspect bounded archived research receipts, never call a provider."""
    if max_files < 1:
        raise ValueError("max_files must be positive")
    root = root.resolve()
    paths = sorted((root / STATUS_PATH).glob("*.json.gz"))
    limited = len(paths) > max_files
    paths = paths[-max_files:]
    fixture_cache = lookup if lookup is not None else CachedFixtureLookup(root)
    provider_schedule = provider_schedule_index(root)
    models = {"XG_PRIMARY": _load_forecasts(primary),
              "ELO_GOALS_FALLBACK_NO_XG": _load_forecasts(fallback)}
    counts = Counter()
    reject = Counter()
    market_count = Counter()
    linked_ids: set[tuple[str, str]] = set()
    scheduled_ids: set[tuple[str, str]] = set()
    price_identities: set[tuple] = set()
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_GZIP_FILE_BYTES:
            reject["UNSAFE_OR_TOO_LARGE_RECEIPT"] += 1
            continue
        try:
            with gzip.open(path, "rb") as f:
                data = f.read(1_000_001)
            if len(data) > 1_000_000:
                raise ValueError("Receipt decompression cap")
            document = json.loads(data)
            offers, _ = extracted_offers(document)
        except (OSError, EOFError, ValueError, TypeError, KeyError):
            reject["UNVERIFIED_SNAPSHOT_INTEGRITY"] += 1
            continue
        counts["integrityVerifiedReceipts"] += 1
        events = {str(row["event"].get("id") or row["event"].get("eventId")):
                  row["event"] for row in document["boundedMarketSnapshot"]}
        resolved: dict[str, tuple[dict | None, str]] = {}
        schedule_resolved: dict[str, tuple[dict | None, str]] = {}
        for offer in offers:
            counts["rawPriceObservations"] += 1
            market, reason = normalized_market(offer)
            if market is None:
                reject[reason] += 1
                continue
            counts["mappedPriceObservations"] += 1
            market_count[market["market"]] += 1
            eid = offer["providerEventId"]
            if eid not in schedule_resolved:
                schedule_resolved[eid] = provider_schedule_resolution(
                    events.get(eid, {}), provider_schedule
                )
            scheduled, schedule_status = schedule_resolved[eid]
            if scheduled is None:
                reject[schedule_status] += 1
            else:
                scheduled_ids.add((scheduled["leagueCode"], eid))
                counts["verifiedSameProviderSchedulePriceObservations"] += 1
            if eid not in resolved:
                resolved[eid] = fixture_resolution(events.get(eid, {}), fixture_cache)
            item, status = resolved[eid]
            if item is None:
                reject[status] += 1
                continue
            linked_ids.add((item["leagueCode"], item["fixtureId"]))
            counts["exactCachedFixturePriceObservations"] += 1
            # Exclude duplicate samples of the same market quote at same receipt
            # (but retain truly separate client observation timestamps).
            unique = (item["leagueCode"], item["fixtureId"], offer["bookmaker"],
                      market["market"], market["direction"], market.get("line"),
                      offer["clientReceivedAtUTC"])
            if unique in price_identities:
                reject["DUPLICATE_SAME_TIME_MARKET_PRICE"] += 1
                continue
            price_identities.add(unique)
            q = {
                **market,
                "leagueCode": item["leagueCode"], "fixtureId": item["fixtureId"],
                "kickoffUTC": item["kickoffUTC"].isoformat(),
                "quoteCutoff": offer["clientReceivedAtUTC"],
                "date": item["kickoffUTC"].astimezone(
                    __import__("zoneinfo").ZoneInfo("Europe/Athens")
                ).date().isoformat(),
                "identityResolution": "EXACT_CACHED_FIXTURE",
            }
            for population, index in models.items():
                forecast, joined = exact_join(q, index)
                if forecast is None:
                    reject[population + "_" + joined] += 1
                    continue
                counts[population + "_prematchHistoricalModelJoined"] += 1
                try:
                    mapped = market_probability(q, forecast)
                except (ValueError, TypeError, KeyError, OverflowError):
                    reject[population + "_INVALID_FORECAST_PROBABILITY"] += 1
                    continue
                if mapped is not None:
                    counts[population + "_researchProbabilitiesMapped"] += 1
    return {
        "contract": "betting-v2-exact-receipt-forecast-bridge-v1",
        "researchOnly": True,
        "providerCalls": 0,
        "certifiedStrong": 0,
        "certifiedEV": None,
        "certifiedROI": None,
        "forecastAsOfQuoteIndependentlyVerified": False,
        "independentBookmakerQuoteTimestampVerified": False,
        "fullBookmakerUniverseVerified": False,
        "readiness": "BLOCKED",
        "receiptArchiveTruncated": limited,
        "receiptFilesScanned": len(paths),
        "distinctExactCachedFixtures": len(linked_ids),
        "distinctSameProviderScheduledFixtures": len(scheduled_ids),
        "sameProviderScheduleIsApiFootballIdentity": False,
        "counts": dict(sorted(counts.items())),
        "mappedByMarket": dict(sorted(market_count.items())),
        "rejected": dict(sorted(reject.items())),
        "note": (
            "Historical model holdout forecasts lack independently recorded "
            "as-of-quote snapshot provenance. Never retroactively treat a "
            "research probability as a dated live recommendation or ROI. "
            "Only same-league exact team/kickoff fixture identities qualify."
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository-root", type=Path, default=Path("."))
    ap.add_argument("--primary-forecast", type=Path)
    ap.add_argument("--elo-forecast", type=Path)
    ap.add_argument("--report", type=Path,
                    default=Path("reports/betting_v2/pilot_receipt_forecast_bridge.json"))
    args = ap.parse_args()
    root = args.repository_root.resolve()
    destination = (root / args.report).resolve()
    if not destination.is_relative_to(root / "reports/betting_v2"):
        raise ValueError("Research output directory required")
    output = study(root, args.primary_forecast, args.elo_forecast)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"contract": output["contract"],
                      "counts": output["counts"],
                      "distinctExactCachedFixtures": output["distinctExactCachedFixtures"],
                      "distinctSameProviderScheduledFixtures": output["distinctSameProviderScheduledFixtures"],
                      "certifiedEV": None, "certifiedROI": None, "providerCalls": 0}))


if __name__ == "__main__":
    main()
