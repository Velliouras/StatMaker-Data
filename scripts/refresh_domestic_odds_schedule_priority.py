#!/usr/bin/env python3
"""Add provider-verified imminent schedule coverage and prioritize its exact-odds refresh.

This wrapper keeps Schedule and Bet responsibilities separate without creating
synthetic prices:
- one global Odds-API.io events request discovers imminent Domestic fixtures;
- leagues with imminent fixtures are moved to the front of the existing odds cycle;
- per-league exact-odds polling is limited to a near-term horizon to avoid wasting
  requests on distant events whose bookmaker markets are not ready yet;
- conservative, league-local team-name containment resolves provider suffixes only
  when they identify exactly one historical canonical team;
- after the exact-odds refresh, every verified imminent event is overlaid into the
  canonical feed, with markets=[] when it is not betting-ready.

No bookmaker price is synthesized, estimated, transformed or copied between events.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Dict, Iterable, List, Sequence

import domestic_live_july_pipeline as pipeline
import refresh_domestic_live_july_odds as target
import refresh_domestic_odds_integrity as guarded


DEFAULT_SCHEDULE_HORIZON_DAYS = 3
DEFAULT_EXACT_ODDS_HORIZON_DAYS = 7
GLOBAL_EVENT_LIMIT = 5000
_GENERIC_TEAM_TOKENS = {
    "club", "fc", "cf", "sc", "ac", "afc", "fk", "bk", "if",
    "de", "da", "do", "dos", "das", "the",
}


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


def _utc_window(days: int) -> tuple[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    start = now.replace(microsecond=0)
    end = (now + dt.timedelta(days=days)).replace(microsecond=0)
    return (
        start.isoformat().replace("+00:00", "Z"),
        end.isoformat().replace("+00:00", "Z"),
    )


def _global_imminent_events(api_key: str, days: int, debug: Dict[str, Any]) -> List[Dict[str, Any]]:
    start, end = _utc_window(days)
    payload = target.odds_fetch.api_get(
        "/events",
        {
            "apiKey": api_key,
            "sport": target.odds_fetch.SPORT,
            "status": "pending,live",
            "from": start,
            "to": end,
            "limit": GLOBAL_EVENT_LIMIT,
        },
        debug,
        allow_error=True,
    )
    rows = [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
    if len(rows) >= GLOBAL_EVENT_LIMIT:
        debug.setdefault("warnings", []).append(
            f"Global imminent event query reached the {GLOBAL_EVENT_LIMIT}-event cap; "
            "Domestic filtering remains fail-safe but the window may be incomplete."
        )
    return rows


def _league_slug(event: Dict[str, Any]) -> str:
    league = event.get("league") if isinstance(event.get("league"), dict) else {}
    return str(league.get("slug") or "").strip()


def _kickoff(event: Dict[str, Any]) -> str:
    return target.odds_fetch.event_kickoff(event)


def _existing_slug_map(registry: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for row in registry:
        slug = str(row.get("providerLeagueSlug") or "").strip()
        code = str(row.get("leagueCode") or "").strip()
        if slug and code:
            mapping[slug] = code

    previous = pipeline.load_json(pipeline.ODDS_PATH, {})
    for league in previous.get("leagues", []) or []:
        if not isinstance(league, dict):
            continue
        slug = str(league.get("providerLeagueSlug") or "").strip()
        code = str(league.get("leagueCode") or "").strip()
        if slug and code:
            mapping.setdefault(slug, code)
    return mapping


def _imminent_codes(events: Iterable[Dict[str, Any]], slug_to_code: Dict[str, str]) -> List[str]:
    earliest: Dict[str, str] = {}
    for event in events:
        code = slug_to_code.get(_league_slug(event))
        kickoff = _kickoff(event)
        if not code or not kickoff:
            continue
        previous = earliest.get(code)
        if previous is None or kickoff < previous:
            earliest[code] = kickoff
    return [code for code, _ in sorted(earliest.items(), key=lambda item: (item[1], item[0]))]


def _install_priority_rotation(priority_codes: Sequence[str]) -> None:
    original = pipeline.rotated
    priority = list(dict.fromkeys(str(code) for code in priority_codes if str(code)))

    def prioritized(items: Sequence[Dict[str, Any]], cursor: int) -> List[Dict[str, Any]]:
        rotated = original(items, cursor)
        by_code = {str(item.get("leagueCode") or ""): item for item in rotated}
        first = [by_code[code] for code in priority if code in by_code]
        first_codes = {str(item.get("leagueCode") or "") for item in first}
        return first + [item for item in rotated if str(item.get("leagueCode") or "") not in first_codes]

    pipeline.rotated = prioritized
    target.pipeline.rotated = prioritized


def _install_near_term_event_horizon(days: int) -> None:
    original = target.odds_fetch.fetch_events_for_league

    def near_term(api_key: str, slug: str, horizon_days: int, debug: Dict[str, Any]) -> List[Dict[str, Any]]:
        return original(api_key, slug, min(max(1, int(horizon_days)), days), debug)

    target.odds_fetch.fetch_events_for_league = near_term


def _meaningful_tokens(text: str) -> set[str]:
    return {
        token for token in text.split()
        if token not in _GENERIC_TEAM_TOKENS and (len(token) >= 4 or token.isdigit())
    }


def _install_conservative_team_mapping() -> None:
    odds = target.odds_fetch

    def canonical_team_info(
        name: str,
        league_code: str,
        aliases: Dict[str, Dict[str, str]],
        debug: Dict[str, Any],
    ) -> tuple[str, str | None]:
        normalized = odds.normalize_text(name, drop_suffixes=True)
        simplified = odds.simplified_team_name(normalized)
        league_aliases = aliases.get(league_code, {})

        for candidate in dict.fromkeys([normalized, simplified]):
            if candidate and candidate in league_aliases:
                canonical = league_aliases[candidate]
                return canonical, canonical

        provider_tokens = set(simplified.split())
        provider_meaningful = _meaningful_tokens(simplified)
        candidates: Dict[str, List[str]] = {}
        if provider_tokens and provider_meaningful:
            for alias_key, canonical in league_aliases.items():
                alias_text = odds.simplified_team_name(alias_key)
                alias_tokens = set(alias_text.split())
                if not alias_tokens:
                    continue
                shared_meaningful = provider_meaningful.intersection(_meaningful_tokens(alias_text))
                if not shared_meaningful:
                    continue
                if alias_tokens.issubset(provider_tokens) or provider_tokens.issubset(alias_tokens):
                    candidates.setdefault(str(canonical), []).append(alias_key)

        if len(candidates) == 1:
            canonical = next(iter(candidates))
            debug.setdefault("conservativeTeamMappings", []).append({
                "leagueCode": league_code,
                "providerTeam": str(name or "").strip(),
                "canonicalTeam": canonical,
                "matchedAliases": sorted(candidates[canonical])[:5],
                "policy": "unique league-local token containment",
            })
            return canonical, canonical

        odds.record_unmatched_team(debug, league_code, str(name or "").strip(), normalized)
        return str(name or "").strip(), None

    target.odds_fetch.canonical_team_info = canonical_team_info


def _install_schedule_only_validation() -> None:
    """Align the odds refresh validator with the existing G2 schedule-only contract."""
    original = target.validate_feed

    def validate(feed: Dict[str, Any], registry: Sequence[Dict[str, Any]], today: dt.date) -> Dict[str, Any]:
        expected = {str(row.get("leagueCode") or "") for row in registry}
        in_scope: List[Dict[str, Any]] = []
        schedule_only_external: List[str] = []

        schedule_only_in_registry = 0
        for league in feed.get("leagues", []) or []:
            if not isinstance(league, dict):
                continue
            code = str(league.get("leagueCode") or "")
            matches = [row for row in league.get("matches", []) or [] if isinstance(row, dict)]
            if code in expected:
                scoped_league = dict(league)
                betting_matches = []
                for row in matches:
                    safe_verified_schedule_only = (
                        row.get("scheduleOnly") is True
                        and row.get("scheduleVerified") is True
                        and str(row.get("scheduleSource") or "").strip()
                        in {"api-football", "odds-api-io-events"}
                        and not (row.get("markets") or [])
                    )
                    if safe_verified_schedule_only:
                        schedule_only_in_registry += 1
                    else:
                        betting_matches.append(row)
                scoped_league["matches"] = betting_matches
                in_scope.append(scoped_league)
                continue

            explicit_schedule_source = (
                str(league.get("providerLeagueSlug") or "").strip() == "api-football-schedule-only"
            )
            safe_schedule_only = explicit_schedule_source and all(
                row.get("scheduleOnly") is True and not (row.get("markets") or [])
                for row in matches
            )
            if not safe_schedule_only:
                raise RuntimeError(
                    f"Unexpected out-of-registry betting league in Domestic feed: {code}"
                )
            schedule_only_external.append(code)

        scoped = dict(feed)
        scoped["leagues"] = in_scope
        result = original(scoped, registry, today)
        result["scheduleOnlyExternalLeagueCount"] = len(schedule_only_external)
        result["scheduleOnlyExternalLeagueCodes"] = sorted(schedule_only_external)
        result["scheduleOnlyInRegistryMatchCount"] = schedule_only_in_registry
        return result

    target.validate_feed = validate


def _match_key(match: Dict[str, Any]) -> tuple[str, ...]:
    match_id = str(match.get("id") or match.get("matchId") or "").strip()
    if match_id:
        return ("id", match_id)
    return (
        "fixture",
        target.odds_fetch.normalize_text(match.get("homeTeam") or match.get("providerHomeTeam"), drop_suffixes=True),
        target.odds_fetch.normalize_text(match.get("awayTeam") or match.get("providerAwayTeam"), drop_suffixes=True),
        str(match.get("date") or match.get("kickoff") or "")[:10],
    )


def _overlay_schedule(
    events: Sequence[Dict[str, Any]],
    registry: Sequence[Dict[str, Any]],
    slug_to_code: Dict[str, str],
    priority_codes: Sequence[str],
    exact_odds_horizon_days: int,
    global_debug: Dict[str, Any],
) -> Dict[str, Any]:
    feed = pipeline.load_json(pipeline.ODDS_PATH, {})
    registry_by_code = {str(row.get("leagueCode") or ""): row for row in registry}
    league_by_code = {
        str(row.get("leagueCode") or ""): row
        for row in feed.get("leagues", []) or []
        if isinstance(row, dict)
    }
    aliases = pipeline.generated_aliases(registry)
    mapping_debug: Dict[str, Any] = {}
    added = updated = bettable = unmapped = domestic_events = 0
    domestic_codes: set[str] = set()

    for event in sorted(events, key=lambda row: (_kickoff(row), target.odds_fetch.event_id(row))):
        slug = _league_slug(event)
        code = slug_to_code.get(slug)
        meta = registry_by_code.get(code or "")
        if not code or meta is None:
            continue
        domestic_events += 1
        domestic_codes.add(code)
        league = league_by_code.get(code)
        if league is None:
            league = {
                "leagueCode": code,
                "country": meta.get("country"),
                "competition": meta.get("competition"),
                "season": meta.get("targetAppSeason"),
                "apiFootballLeagueId": meta.get("apiFootballLeagueId"),
                "enabledForStats": bool(meta.get("enabledForStats", True)),
                "enabledForOdds": bool(meta.get("enabledForOdds", True)),
                "enabledForBetting": bool(meta.get("enabledForBetting", True)),
                "providerLeagueSlug": slug,
                "matches": [],
            }
            feed.setdefault("leagues", []).append(league)
            league_by_code[code] = league
        elif not str(league.get("providerLeagueSlug") or "").strip():
            league["providerLeagueSlug"] = slug

        fresh = target.odds_fetch.normalize_event_match(
            meta,
            event,
            event,
            aliases,
            mapping_debug,
        )
        if not fresh:
            continue
        fresh["scheduleOnly"] = True
        fresh["scheduleSource"] = "odds-api-io-events"
        fresh["scheduleVerified"] = True
        fresh["markets"] = []

        matches = [row for row in league.get("matches", []) or [] if isinstance(row, dict)]
        by_key = {_match_key(row): row for row in matches}
        key = _match_key(fresh)
        current = by_key.get(key)
        if current is None:
            matches.append(fresh)
            added += 1
            if fresh.get("teamMappingStatus") != "matched":
                unmapped += 1
        else:
            refreshed = dict(fresh)
            refreshed.update(current)
            refreshed["date"] = fresh.get("date")
            refreshed["kickoff"] = fresh.get("kickoff")
            refreshed["scheduleSource"] = "odds-api-io-events"
            refreshed["scheduleVerified"] = True
            if current.get("markets"):
                refreshed["scheduleOnly"] = False
                bettable += 1
            else:
                refreshed["scheduleOnly"] = True
            index = matches.index(current)
            matches[index] = refreshed
            updated += 1
        league["matches"] = sorted(
            matches,
            key=lambda row: (str(row.get("kickoff") or row.get("date") or ""), str(row.get("homeTeam") or "")),
        )

    coverage = {
        "source": "Odds-API.io global events endpoint",
        "syntheticOdds": False,
        "globalEventsFetched": len(events),
        "domesticEventsMatched": domestic_events,
        "domesticLeagueCount": len(domestic_codes),
        "domesticLeagueCodes": sorted(domestic_codes),
        "priorityLeagueCodes": list(priority_codes),
        "scheduleOnlyMatchesAdded": added,
        "existingMatchesUpdated": updated,
        "existingBettableMatchesConfirmed": bettable,
        "scheduleMatchesWithoutHistoricalMapping": unmapped,
        "exactOddsEventHorizonDays": exact_odds_horizon_days,
        "globalEventWarnings": global_debug.get("warnings", []),
    }
    feed.setdefault("debug", {})["domesticScheduleCoverage"] = coverage
    pipeline.write_json(pipeline.ODDS_PATH, feed)

    report = pipeline.load_json(target.REPORT_PATH, {})
    report["scheduleCoverage"] = coverage
    report["priorityLeagueCodes"] = list(priority_codes)
    report["exactOddsEventHorizonDays"] = exact_odds_horizon_days
    pipeline.write_json(target.REPORT_PATH, report)
    return coverage


def main() -> int:
    api_key = os.getenv("ODDS_API_IO_KEY", "").strip()
    if not api_key:
        print("ERROR: ODDS_API_IO_KEY is required.")
        return 2

    registry_payload = pipeline.load_json(pipeline.REGISTRY_PATH, {})
    registry = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    if not registry:
        print("ERROR: Domestic registry is empty; refusing schedule overlay.")
        return 3

    schedule_days = _positive_int("STATMAKER_DOMESTIC_SCHEDULE_HORIZON_DAYS", DEFAULT_SCHEDULE_HORIZON_DAYS)
    odds_days = _positive_int("STATMAKER_DOMESTIC_EXACT_ODDS_HORIZON_DAYS", DEFAULT_EXACT_ODDS_HORIZON_DAYS)
    global_debug: Dict[str, Any] = {"warnings": [], "apiCalls": []}
    events = _global_imminent_events(api_key, schedule_days, global_debug)
    slug_to_code = _existing_slug_map(registry)
    priority_codes = _imminent_codes(events, slug_to_code)

    _install_priority_rotation(priority_codes)
    _install_near_term_event_horizon(odds_days)
    _install_conservative_team_mapping()
    _install_schedule_only_validation()

    result = guarded.main()
    if result != 0:
        return result

    coverage = _overlay_schedule(
        events,
        registry,
        slug_to_code,
        priority_codes,
        odds_days,
        global_debug,
    )
    print(json.dumps({
        "scheduleCoverage": coverage,
        "priorityLeagueCodes": priority_codes,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
