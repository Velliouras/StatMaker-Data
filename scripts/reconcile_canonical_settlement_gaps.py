#!/usr/bin/env python3
"""Reconcile canonical settlements by exact API-Football fixture identity.

The canonical recommendation ledger is the authoritative recommendation identity. This stage runs
before the broader date-driven settlement collector and fetches only canonical rows that have an
exact API-Football fixture id. It covers both today's finished matches and past gaps, never guesses
by team name, and fails closed when the provider fixture identity disagrees with the canonical row.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from typing import Any, Dict, List, Sequence, Tuple
from zoneinfo import ZoneInfo

import api_football_daily_quota_guard as quota_guard
import api_football_fetch_fixture_stats as stats_fetch
import refresh_fixture_validity as validity
import refresh_live_settlements as live

ATHENS = ZoneInfo("Europe/Athens")
LEDGER_PATH = live.ROOT / "data" / "statmaker" / "canonical_recommendation_ledger.json"
MAX_IDS_PER_REQUEST = 20


def _identity_valid(home_names: Sequence[str], away_names: Sequence[str]) -> bool:
    home = {live.normalize_team(value) for value in home_names if live.normalize_team(value)}
    away = {live.normalize_team(value) for value in away_names if live.normalize_team(value)}
    return bool(home and away and home.isdisjoint(away))


def _names(item: Dict[str, Any], list_key: str, fallback_key: str) -> Tuple[str, ...]:
    values = item.get(list_key)
    if not isinstance(values, list):
        values = [item.get(fallback_key)]
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = live.normalize_team(text)
        if text and key and key not in seen:
            out.append(text)
            seen.add(key)
    return tuple(out)


def ledger_requirements(today: dt.date) -> List[live.SettlementRequirement]:
    root = live.load_json(LEDGER_PATH, {})
    if not isinstance(root, dict) or int(root.get("schemaVersion") or 0) < 4:
        return []
    cutoff = today - dt.timedelta(days=live.RETENTION_DAYS)
    result: List[live.SettlementRequirement] = []
    for item in root.get("entries", []) or []:
        if not isinstance(item, dict):
            continue
        day_text = str(item.get("localDate") or "").strip()[:10]
        try:
            day = dt.date.fromisoformat(day_text)
        except ValueError:
            continue
        # Exact-id reconciliation is authoritative for both same-day and past canonical rows.
        # Future fixtures are not settlement candidates.
        if day < cutoff or day > today:
            continue
        fixture_id = live.as_int(item.get("apiFixtureId"))
        if fixture_id is None:
            continue
        home_names = _names(item, "homeNames", "homeTeam")
        away_names = _names(item, "awayNames", "awayTeam")
        if not _identity_valid(home_names, away_names):
            continue
        sub_market = str(item.get("subMarketKey") or "").strip()
        required = str(item.get("requiredKind") or live.SUBMARKET_REQUIREMENT.get(sub_market, "unsupported")).strip()
        if required == "unsupported":
            continue
        result.append(
            live.SettlementRequirement(
                generation_id=str(item.get("generationId") or "").strip(),
                competition_id=str(item.get("competitionId") or "").strip(),
                match_key=str(item.get("matchKey") or "").strip(),
                local_date=day_text,
                league_code=str(item.get("leagueCode") or "").strip().upper(),
                api_fixture_id=fixture_id,
                home_names=home_names,
                away_names=away_names,
                required_kind=required,
                sub_market_key=sub_market,
            )
        )
    return result


def _disposed_keys(feed: Dict[str, Any]) -> set[Tuple[str, str, str, str]]:
    result: set[Tuple[str, str, str, str]] = set()
    for item in feed.get("fixtureDispositions", []) if isinstance(feed, dict) else []:
        if not isinstance(item, dict):
            continue
        result.add((
            str(item.get("competitionId") or "").strip(),
            str(item.get("matchKey") or "").strip(),
            str(item.get("localDate") or "").strip()[:10],
            str(item.get("leagueCode") or "").strip().upper(),
        ))
    return result


def _requirement_key(row: live.SettlementRequirement) -> Tuple[str, str, str, str]:
    return (row.competition_id, row.match_key, row.local_date, row.league_code)


def _feed_rows(cache_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "fixtureId": item.get("fixtureId"),
            "dateUtc": item.get("dateUtc"),
            "leagueCode": item.get("leagueCode"),
            "country": item.get("country"),
            "competition": item.get("competition"),
            "season": item.get("season"),
            "homeTeam": item.get("homeTeam"),
            "awayTeam": item.get("awayTeam"),
            "status": item.get("status"),
            "homeGoals": item.get("homeGoals"),
            "awayGoals": item.get("awayGoals"),
            "homeHalfGoals": item.get("homeHalfGoals"),
            "awayHalfGoals": item.get("awayHalfGoals"),
            "normalizedStats": item.get("normalizedStats") or {},
            "requiredStats": item.get("requiredStats") or [],
            "missingRequiredStats": live.missing_required_kinds(
                item.get("normalizedStats"), item.get("requiredStats") or []
            ),
        }
        for item in cache_rows
        if str(item.get("status") or "").upper() in live.COMPLETED
        and item.get("homeGoals") is not None
        and item.get("awayGoals") is not None
    ]


def _chunks(values: Sequence[int], size: int) -> List[List[int]]:
    return [list(values[index:index + size]) for index in range(0, len(values), size)]


def _league_compatible(left: str, right: str) -> bool:
    a = str(left or "").strip().upper()
    b = str(right or "").strip().upper()
    if not a or not b or a == b:
        return True
    return {a, b} <= {"CONF", "UECL"}


def _provider_fixture_matches(
    fixture: Dict[str, Any],
    registry_row: Dict[str, Any],
    requirement: live.SettlementRequirement,
) -> bool:
    fixture_id = stats_fetch.fixture_identity(fixture)
    if requirement.api_fixture_id is None or fixture_id != requirement.api_fixture_id:
        return False
    summary = stats_fetch.fixture_summary(fixture)
    home = str(summary.get("home_team") or "").strip()
    away = str(summary.get("away_team") or "").strip()
    provider_league = str(registry_row.get("leagueCode") or "").strip().upper()
    if not _league_compatible(requirement.league_code, provider_league):
        return False
    return live.team_matches(home, requirement.home_names) and live.team_matches(away, requirement.away_names)


def _cached_row_satisfies(
    row: Dict[str, Any],
    requirements: Sequence[live.SettlementRequirement],
) -> bool:
    if str(row.get("status") or "").upper() not in live.COMPLETED:
        return False
    if row.get("homeGoals") is None or row.get("awayGoals") is None:
        return False
    home = str(row.get("homeTeam") or "").strip()
    away = str(row.get("awayTeam") or "").strip()
    league = str(row.get("leagueCode") or "").strip().upper()
    for requirement in requirements:
        if not _league_compatible(requirement.league_code, league):
            return False
        if not live.team_matches(home, requirement.home_names):
            return False
        if not live.team_matches(away, requirement.away_names):
            return False
    required_kinds = {r.required_kind for r in requirements if r.required_kind in live.REQUIRED_FIELDS}
    return not live.missing_required_kinds(row.get("normalizedStats"), required_kinds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile canonical settlements by exact fixture id")
    parser.add_argument("--max-requests", type=int, default=8)
    args = parser.parse_args()
    max_requests = max(0, args.max_requests)

    today = live.now_utc().astimezone(ATHENS).date()
    requirements = ledger_requirements(today)
    existing_root = live.load_json(live.CACHE_PATH, {})
    cache = live.cached_map(existing_root if isinstance(existing_root, dict) else {})
    live.prune_cache(cache, today)
    feed_root = live.load_json(live.FEED_PATH, {})
    if not isinstance(feed_root, dict):
        feed_root = {}
    disposed = _disposed_keys(feed_root)

    by_id: Dict[int, List[live.SettlementRequirement]] = {}
    for row in requirements:
        fixture_id = row.api_fixture_id
        if fixture_id is None or _requirement_key(row) in disposed:
            continue
        by_id.setdefault(fixture_id, []).append(row)

    # Do not spend provider quota on an exact fixture already cached as completed with the same
    # canonical home/away identity and all required detailed statistics present.
    pending_by_id: Dict[int, List[live.SettlementRequirement]] = {}
    cache_identity_rejected = 0
    for fixture_id, rows in by_id.items():
        cached = cache.get(fixture_id)
        if cached is not None and _cached_row_satisfies(cached, rows):
            continue
        if cached is not None:
            # A cache entry with the right numeric id but incompatible canonical identity must never
            # survive as a settlement source. Re-fetch that exact id and replace only after verify.
            cache.pop(fixture_id, None)
            cache_identity_rejected += 1
        pending_by_id[fixture_id] = rows

    if not pending_by_id or max_requests == 0:
        print(
            "canonical-settlement-exact "
            f"canonicalExact={len(requirements)} pendingExact={len(pending_by_id)} requests=0 "
            f"captured=0 identityRejected={cache_identity_rejected} statsRequests=0"
        )
        return 0

    api_key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required", file=os.sys.stderr)
        return 2
    quota_guard.install(stats_fetch)

    registry = live.registry_rows()
    rows_by_provider: Dict[int, List[Dict[str, Any]]] = {}
    for row in registry:
        provider_id = live.as_int(row.get("api_football_league_id") or row.get("apiFootballLeagueId"))
        if provider_id is not None:
            rows_by_provider.setdefault(provider_id, []).append(row)

    # Oldest unresolved day first, then fixture id. A single API call can verify up to 20 exact ids.
    ordered_ids = sorted(
        pending_by_id,
        key=lambda fixture_id: (min(row.local_date for row in pending_by_id[fixture_id]), fixture_id),
    )
    request_state = {"count": 0}
    captured = 0
    stats_requests = 0
    nonfinal = 0
    missing_provider = 0
    provider_identity_rejected = 0

    for id_chunk in _chunks(ordered_ids, MAX_IDS_PER_REQUEST):
        if request_state["count"] >= max_requests:
            break
        try:
            fixtures = validity.fetch_by_ids(api_key, id_chunk, request_state, max_requests)
        except stats_fetch.RequestLimitReached:
            break
        except Exception as error:
            print(f"canonical-settlement-exact ids fetch failed: {error}", file=os.sys.stderr)
            continue
        fixture_by_id = {
            stats_fetch.fixture_identity(fixture): fixture
            for fixture in fixtures
            if stats_fetch.fixture_identity(fixture) is not None
        }
        for fixture_id in id_chunk:
            fixture = fixture_by_id.get(fixture_id)
            if fixture is None:
                missing_provider += 1
                continue
            if stats_fetch.fixture_status_short(fixture).upper() not in live.COMPLETED:
                nonfinal += 1
                continue
            registry_row = live.choose_registry_row(fixture, rows_by_provider)
            candidate_requirements = pending_by_id.get(fixture_id, [])
            if registry_row is None or not candidate_requirements:
                continue

            # Exact numeric id is necessary but not sufficient. The provider's home/away and league
            # must agree with every canonical row attached to that id; otherwise fail closed.
            if not all(
                _provider_fixture_matches(fixture, registry_row, requirement)
                for requirement in candidate_requirements
            ):
                provider_identity_rejected += 1
                summary = stats_fetch.fixture_summary(fixture)
                print(
                    "canonical-settlement-exact IDENTITY_REJECTED "
                    f"fixtureId={fixture_id} provider={summary.get('home_team')} vs {summary.get('away_team')} "
                    f"canonical=" + ";".join(
                        f"{r.home_names[0]} vs {r.away_names[0]}" for r in candidate_requirements
                    ),
                    file=os.sys.stderr,
                )
                continue

            row = live.settlement_row(fixture, registry_row, cache.get(fixture_id, {}), candidate_requirements)
            if row.get("homeGoals") is None or row.get("awayGoals") is None:
                continue
            if live.fetch_required_stats(api_key, fixture, row, request_state, max_requests):
                stats_requests += 1
            cache[fixture_id] = row
            captured += 1

    ordered = sorted(
        cache.values(),
        key=lambda item: (str(item.get("dateUtc") or ""), int(item.get("fixtureId") or 0)),
    )
    cache_payload = {
        "schemaVersion": max(live.SCHEMA_VERSION, int(existing_root.get("schemaVersion") or 0))
        if isinstance(existing_root, dict) else live.SCHEMA_VERSION,
        "generatedAt": live.iso_now(),
        "retentionDays": live.RETENTION_DAYS,
        "canonicalRequirementCount": max(
            len(requirements),
            int(existing_root.get("canonicalRequirementCount") or 0) if isinstance(existing_root, dict) else 0,
        ),
        "fixtures": ordered,
    }
    cache_changed = live.write_json_if_changed(live.CACHE_PATH, cache_payload, "fixtures")

    feed_rows = _feed_rows(ordered)
    feed_payload = dict(feed_root)
    feed_payload.update({
        "schemaVersion": max(3, int(feed_root.get("schemaVersion") or 0), live.SCHEMA_VERSION),
        "generatedAt": live.iso_now(),
        "source": "api-football-live-settlement",
        "completedStatuses": sorted(live.COMPLETED),
        "retentionDays": live.RETENTION_DAYS,
        "fixtures": feed_rows,
    })
    feed_changed = live.write_json_if_changed(live.FEED_PATH, feed_payload, "fixtures")

    print(
        "canonical-settlement-exact "
        f"canonicalExact={len(requirements)} pendingExact={len(pending_by_id)} captured={captured} "
        f"nonFinal={nonfinal} missingProvider={missing_provider} "
        f"identityRejected={cache_identity_rejected + provider_identity_rejected} "
        f"statsRequests={stats_requests} feedFixtures={len(feed_rows)} requests={request_state['count']} "
        f"cacheChanged={cache_changed} feedChanged={feed_changed} quota={json.dumps(quota_guard.status())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
