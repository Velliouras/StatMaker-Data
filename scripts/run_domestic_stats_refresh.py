#!/usr/bin/env python3
"""Quota-aware Domestic Stats orchestration with verified historical closure.

Historical seasons are not considered closed merely because the local cache looks complete.
A historical snapshot is frozen only after an exact API-Football ``league + season`` discovery
confirms the completed fixture set, every discovered completed fixture is present locally with a
final score, and every fixture has had its statistics endpoint attempted at least once.

Once verified, the snapshot is frozen and future scheduled runs use zero API calls for it.
Current seasons remain incremental.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping

import api_football_daily_quota_guard as quota_guard
import refresh_domestic_live_july_stats as target
import statmaker_domestic_scope as scope

ROOT = Path(__file__).resolve().parents[1]
FREEZE_PATH = ROOT / "data" / "statmaker" / "domestic_historical_stats_freeze.json"
REPORT_PATH = ROOT / "reports" / "domestic_live_july_stats_refresh.json"
ROSTER_PATH = ROOT / "data" / "statmaker" / "domestic_rosters.json"
FREEZE_SCHEMA_VERSION = 2
FREEZE_POLICY = (
    "historical season closes only after exact league+season fixture discovery, complete score cache, "
    "and one statistics-endpoint attempt per completed fixture"
)


def normalize_code(value: Any) -> str:
    return scope.normalize_code(value)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def historical_season(league: Mapping[str, Any]) -> str:
    return str(
        league.get("historyApiSeason")
        or league.get("season")
        or ""
    ).strip()


def target_season(league: Mapping[str, Any]) -> str:
    return str(
        league.get("targetApiSeason")
        or league.get("target_api_football_season")
        or ""
    ).strip()


def is_historical_snapshot(league: Mapping[str, Any]) -> bool:
    history = historical_season(league)
    current = target_season(league)
    return bool(history and current and history != current)


def freeze_key(league: Mapping[str, Any]) -> str:
    return f"{normalize_code(league.get('leagueCode'))}:{historical_season(league)}"


def completed_cache_rows(league: Mapping[str, Any]) -> List[Dict[str, Any]]:
    cache = load_json(target.stats_fetch.cache_path_for(dict(league)), {})
    return [
        item
        for item in cache.get("fixtures", []) or []
        if isinstance(item, dict)
        and str(item.get("status") or item.get("status_short") or "").upper()
        in target.COMPLETED_STATUSES
    ]


def target_app_season(league: Mapping[str, Any]) -> str:
    return str(
        league.get("targetAppSeason")
        or league.get("target_app_season")
        or ""
    ).strip()


def roster_catalog_entries(raw: Any) -> Dict[tuple[str, str], Dict[str, Any]]:
    payload = raw if isinstance(raw, dict) else {}
    rows = payload.get("leagues", [])
    result: Dict[tuple[str, str], Dict[str, Any]] = {}
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        code = normalize_code(item.get("leagueCode"))
        season = str(item.get("appSeason") or "").strip()
        teams = [str(name).strip() for name in item.get("teams", []) if str(name).strip()]
        if code and season and teams:
            result[(code, season)] = dict(item)
    return result


def discover_missing_target_rosters(
    api_key: str,
    leagues: List[Dict[str, Any]],
    max_requests: int,
) -> int:
    catalog = load_json(ROSTER_PATH, {})
    entries = roster_catalog_entries(catalog)
    missing = [
        league for league in leagues
        if target_season(league)
        and target_app_season(league)
        and (normalize_code(league.get("leagueCode")), target_app_season(league)) not in entries
        and target_season(league) != historical_season(league)
    ]
    missing.sort(
        key=lambda league: (
            scope.priority_rank(league),
            str(league.get("targetSeasonStart") or ""),
            normalize_code(league.get("leagueCode")),
        )
    )
    if not missing or max_requests <= 1:
        return 0

    # Stay inside the caller's existing request ceiling. At the normal 2400-request run
    # this is enough to discover every rollover roster in one pass; small targeted runs
    # reserve almost all of their budget for the historical/statistics work they requested.
    roster_request_cap = min(len(missing), max(1, max_requests // 20))
    request_state = {"count": 0}

    for league in missing[:roster_request_cap]:
        if request_state["count"] >= roster_request_cap:
            break
        try:
            league_id = int(league.get("apiFootballLeagueId") or league.get("api_football_league_id"))
            season = int(target_season(league))
        except (TypeError, ValueError):
            continue
        try:
            payload = target.stats_fetch.api_get(
                api_key,
                "fixtures",
                {"league": league_id, "season": season},
                request_state,
                roster_request_cap,
            )
        except target.stats_fetch.RequestLimitReached:
            break
        except Exception:
            continue

        fixtures = target.stats_fetch.response_items(payload)
        teams = target.stats_fetch.roster_from_fixtures(fixtures)
        if not teams:
            continue
        code = normalize_code(league.get("leagueCode"))
        app_season = target_app_season(league)
        entries[(code, app_season)] = {
            "leagueCode": code,
            "country": league.get("country"),
            "competition": league.get("competition"),
            "appSeason": app_season,
            "apiFootballSeason": str(season),
            "source": "api-football exact target league+season fixture discovery",
            "teams": teams,
        }

    write_json(ROSTER_PATH, {
        "schemaVersion": 1,
        "generatedAt": target.pipeline.now_utc(),
        "source": "api-football",
        "contract": (
            "league rosters discovered from exact league+season fixture responses; "
            "target-roster discovery stays inside the existing Domestic Stats request ceiling"
        ),
        "leagueCount": len(entries),
        "leagues": [
            entries[key]
            for key in sorted(entries, key=lambda item: (item[0], item[1]))
        ],
    })
    return request_state["count"]


def migrate_freeze_contract(raw: Any) -> Dict[str, Any]:
    freeze = raw if isinstance(raw, dict) else {}
    snapshots = freeze.setdefault("snapshots", {})
    old_schema = int(freeze.get("schemaVersion") or 0)

    # Schema v1 could freeze a truncated cache (the Panathinaikos 15-match case) because it
    # validated only the rows already present locally. Invalidate every such legacy freeze once.
    if old_schema < FREEZE_SCHEMA_VERSION:
        for row in snapshots.values():
            if not isinstance(row, dict):
                continue
            if row.get("frozen") is True and row.get("fixtureSetVerified") is not True:
                row["frozen"] = False
                row["invalidatedReason"] = "legacy freeze lacked exact provider fixture-set verification"

    freeze["schemaVersion"] = FREEZE_SCHEMA_VERSION
    freeze["policy"] = FREEZE_POLICY
    return freeze


def exact_fixture_discovery(
    api_key: str,
    league: Mapping[str, Any],
    request_state: Dict[str, int],
    max_requests: int,
) -> tuple[bool, List[Dict[str, Any]], str]:
    season = historical_season(league)
    league_id = league.get("api_football_league_id") or league.get("apiFootballLeagueId")
    if not season or league_id is None:
        return False, [], "missing league id or historical season"

    try:
        payload = target.stats_fetch.api_get(
            api_key,
            "fixtures",
            {"league": int(league_id), "season": int(season)},
            request_state,
            max_requests,
        )
    except target.stats_fetch.RequestLimitReached:
        return False, [], "request cap or daily reserve reached before exact fixture verification"
    except Exception as exc:
        return False, [], f"exact fixture verification failed: {type(exc).__name__}: {exc}"

    errors = target.stats_fetch.api_errors(payload)
    if errors:
        return False, [], f"provider errors during exact fixture verification: {errors}"

    all_rows = target.stats_fetch.response_items(payload)
    completed = [row for row in all_rows if target.stats_fetch.is_completed_fixture(row)]
    if not all_rows:
        return False, [], "exact league+season query returned no fixtures"
    if not completed:
        return False, [], "exact league+season query returned no completed fixtures"
    return True, completed, f"league+season:{season} returned={len(all_rows)} completed={len(completed)}"


def verify_historical_snapshot(
    api_key: str,
    league: Dict[str, Any],
    request_state: Dict[str, int],
    max_requests: int,
) -> tuple[bool, Dict[str, Any]]:
    verified, provider_completed, note = exact_fixture_discovery(
        api_key, league, request_state, max_requests
    )

    cache_path = target.stats_fetch.cache_path_for(league)
    cache = load_json(cache_path, {})
    cached_by_id = target.stats_fetch.cached_fixture_map(cache)
    provider_ids = {
        fixture_id
        for row in provider_completed
        if (fixture_id := target.stats_fetch.fixture_identity(row)) is not None
    }
    cached_ids = set(cached_by_id)
    missing_ids = sorted(provider_ids - cached_ids)
    extra_ids = sorted(cached_ids - provider_ids) if verified else []

    # Once exact provider discovery succeeds, historical cache identity becomes the provider's
    # exact completed fixture set. This removes stale/fallback contamination from prior runs.
    if verified and not missing_ids:
        exact_rows = [cached_by_id[fixture_id] for fixture_id in sorted(provider_ids)]
        target.stats_fetch.write_json(cache_path, target.stats_fetch.cache_payload(league, exact_rows))
        cached_by_id = {fixture_id: cached_by_id[fixture_id] for fixture_id in provider_ids}

    rows = [
        row for row in cached_by_id.values()
        if str(row.get("status") or row.get("status_short") or "").upper() in target.COMPLETED_STATUSES
    ]
    with_scores = sum(1 for row in rows if target.has_final_score(row))
    stats_attempted = sum(1 for row in rows if "raw_statistics" in row)
    real_stats = sum(1 for row in rows if target.has_real_normalized_stats(row))

    summary: Dict[str, Any] = {
        "fixtureSetVerified": bool(verified and provider_ids and not missing_ids),
        "verificationQuery": f"league+season:{historical_season(league)}",
        "verificationNote": note,
        "providerCompletedFixtures": len(provider_ids),
        "cachedCompletedFixtures": len(rows),
        "missingDiscoveredFixtureCount": len(missing_ids),
        "missingDiscoveredFixtureIds": missing_ids[:50],
        "extraCachedFixturesPruned": len(extra_ids) if verified and not missing_ids else 0,
        "withScores": with_scores,
        "statsAttempted": stats_attempted,
        "withRealStats": real_stats,
        "providerEmptyStats": max(0, stats_attempted - real_stats),
    }

    ready = (
        summary["fixtureSetVerified"] is True
        and len(rows) == len(provider_ids)
        and with_scores == len(rows)
        and stats_attempted == len(rows)
    )
    return ready, summary


def parse_codes(raw: str) -> set[str]:
    return {
        normalize_code(code)
        for code in str(raw or "").replace(";", ",").split(",")
        if normalize_code(code)
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Domestic Stats refresh with verified historical closure")
    parser.add_argument("--max-requests", type=int, default=target.DEFAULT_MAX_REQUESTS)
    parser.add_argument(
        "--league-codes",
        default=os.getenv("STATMAKER_STATS_LEAGUE_CODES", ""),
        help="Optional comma-separated Domestic league codes, e.g. G1 or E0,G1",
    )
    parser.add_argument(
        "--historical-only",
        action="store_true",
        help="Process only configured completed historical snapshots that are not yet verified/frozen",
    )
    parser.add_argument(
        "--skip-roster-discovery",
        action="store_true",
        help="Do not discover current target-season rosters in this run",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    if not api_key:
        print("ERROR: API_FOOTBALL_KEY is required.", file=sys.stderr)
        return 2

    quota_guard.install(target.stats_fetch)
    max_requests = max(1, int(args.max_requests))
    target.scope.install_stats_registry_load_guard(target.pipeline)
    registry_payload = target.pipeline.load_json(target.pipeline.REGISTRY_PATH, {})
    registry = registry_payload.get("leagues", []) if isinstance(registry_payload, dict) else []
    registry = scope.filter_stats_leagues(registry)
    if not registry:
        print("ERROR: Domestic Stats registry is empty.", file=sys.stderr)
        return 3

    requested_codes = parse_codes(args.league_codes)
    selected = [
        league for league in registry
        if not requested_codes or normalize_code(league.get("leagueCode")) in requested_codes
    ]
    if args.historical_only:
        selected = [league for league in selected if is_historical_snapshot(league)]

    if requested_codes:
        found = {normalize_code(league.get("leagueCode")) for league in selected}
        missing = sorted(requested_codes - found)
        if missing:
            print(f"ERROR: requested league codes not present in selected Stats scope: {missing}", file=sys.stderr)
            return 4
    if not selected:
        print("ERROR: no Domestic Stats leagues selected.", file=sys.stderr)
        return 5

    freeze = migrate_freeze_contract(load_json(FREEZE_PATH, {}))
    snapshots = freeze.setdefault("snapshots", {})

    all_historical = [league for league in registry if is_historical_snapshot(league)]
    pending_before = [
        freeze_key(league)
        for league in all_historical
        if not (
            snapshots.get(freeze_key(league), {}).get("frozen") is True
            and snapshots.get(freeze_key(league), {}).get("fixtureSetVerified") is True
        )
    ]

    skipped_frozen: List[str] = []
    active: List[Dict[str, Any]] = []
    for league in selected:
        key = freeze_key(league)
        frozen_row = snapshots.get(key, {})
        if (
            is_historical_snapshot(league)
            and frozen_row.get("frozen") is True
            and frozen_row.get("fixtureSetVerified") is True
        ):
            skipped_frozen.append(key)
        else:
            active.append(league)

    roster_requests = (
        0
        if args.skip_roster_discovery
        else discover_missing_target_rosters(api_key, selected, max_requests)
    )
    historical_active = [league for league in active if is_historical_snapshot(league)]
    remaining_after_rosters = max(1, max_requests - roster_requests)
    verification_reserve = min(len(historical_active), max(0, remaining_after_rosters - 1))
    refresh_budget = max(1, remaining_after_rosters - verification_reserve)

    if active:
        report = target.refresh_incrementally(api_key, active, refresh_budget)
    else:
        report = {
            "generatedAt": target.pipeline.now_utc(),
            "completenessContract": "all completed fixture scores are cached at discovery time; advanced statistics are incrementally backfilled",
            "incrementalContract": "current seasons incremental; historical snapshots close only after exact provider fixture-set verification",
            "scopeContract": "69 configured Domestic leagues eligible for Stats; targeted/historical-only mode may refresh a subset",
            "priorityPolicy": "Verified frozen historical snapshots use zero future API requests",
            "absolutePriorityLeagueCodes": sorted(scope.absolute_priority_codes()),
            "statsUniverseLeagueCount": len(scope.stats_universe_codes()),
            "coreOddsLeagueCount": len(scope.core_odds_codes()),
            "maxRequests": max_requests,
            "requestsUsed": 0,
            "registryLeagueCount": len(selected),
            "polledLeagueCount": 0,
            "incompleteBefore": 0,
            "completeAfter": 0,
            "incompleteAfter": 0,
            "allocations": [],
            "progress": [],
        }

    verification_state = {"count": roster_requests + int(report.get("requestsUsed") or 0)}
    newly_frozen: List[str] = []
    verification_pending: List[Dict[str, Any]] = []

    for league in historical_active:
        key = freeze_key(league)
        ready, summary = verify_historical_snapshot(
            api_key, league, verification_state, max_requests
        )
        if ready:
            snapshots[key] = {
                "frozen": True,
                "leagueCode": normalize_code(league.get("leagueCode")),
                "country": league.get("country"),
                "competition": league.get("competition"),
                "historyApiSeason": historical_season(league),
                "targetApiSeasonAtFreeze": target_season(league),
                "frozenAt": target.pipeline.now_utc(),
                **summary,
            }
            newly_frozen.append(key)
        else:
            snapshots[key] = {
                **(snapshots.get(key, {}) if isinstance(snapshots.get(key), dict) else {}),
                "frozen": False,
                "leagueCode": normalize_code(league.get("leagueCode")),
                "country": league.get("country"),
                "competition": league.get("competition"),
                "historyApiSeason": historical_season(league),
                "lastCheckedAt": target.pipeline.now_utc(),
                **summary,
            }
            verification_pending.append({"snapshot": key, **summary})

    pending_after = [
        freeze_key(league)
        for league in all_historical
        if not (
            snapshots.get(freeze_key(league), {}).get("frozen") is True
            and snapshots.get(freeze_key(league), {}).get("fixtureSetVerified") is True
        )
    ]

    freeze["generatedAt"] = target.pipeline.now_utc()
    freeze["historicalSnapshotCount"] = len(all_historical)
    freeze["frozenSnapshotCount"] = len(all_historical) - len(pending_after)
    freeze["pendingSnapshotCount"] = len(pending_after)
    freeze["closureComplete"] = len(pending_after) == 0
    freeze["pendingSnapshots"] = pending_after
    write_json(FREEZE_PATH, freeze)

    report["rosterDiscoveryRequests"] = roster_requests
    report["requestsUsed"] = verification_state["count"]
    report["maxRequests"] = max_requests
    report["selectedLeagueCodes"] = [normalize_code(league.get("leagueCode")) for league in selected]
    report["targetedMode"] = bool(requested_codes)
    report["historicalOnlyMode"] = bool(args.historical_only)
    report["historicalFreezePolicy"] = FREEZE_POLICY
    report["historicalSkippedFrozen"] = skipped_frozen
    report["historicalNewlyFrozen"] = newly_frozen
    report["historicalVerificationPending"] = verification_pending
    report["historicalSnapshotCount"] = len(all_historical)
    report["historicalPendingBefore"] = pending_before
    report["historicalPendingAfter"] = pending_after
    report["historicalFrozenSnapshotCount"] = freeze["frozenSnapshotCount"]
    report["historicalClosureComplete"] = freeze["closureComplete"]
    report["apiFootballQuotaGuard"] = quota_guard.status()
    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
